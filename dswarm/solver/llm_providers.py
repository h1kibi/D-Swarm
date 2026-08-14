"""Core LLM provider registry for Worker and ReasonSwarm credential binding.

Providers are non-secret endpoint templates plus a write-only API key stored in
``sessions/_secrets/llm_providers/<provider_id>/API_KEY``.  They are used by Pi
workers and by host-side ReasonSwarm helper LLMs so the operator configures a
relay once and then binds profiles to it by ``provider_ref``.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from dswarm.solver.endpoint_probe import normalize_base_url, probe_endpoint
from dswarm.solver.secret_store import atomic_write, chmod_private_dir, updated_at

_PROVIDER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_VALID_WIRE_APIS = {"auto", "openai", "openai-chat", "openai-responses"}
_VALID_AUTH_MODES = {"bearer", "x-api-key", "custom"}

DEFAULT_PROVIDER_TEMPLATES: list[dict[str, Any]] = [
    {"id": "deepseek", "label": "DeepSeek", "base_url": "https://api.deepseek.com/v1", "wire_api": "auto", "auth_mode": "bearer", "auth_header": "Authorization", "auth_prefix": "Bearer", "models": ["deepseek-v4-pro", "deepseek-v4-flash", "deepseek-chat", "deepseek-reasoner"]},
    {"id": "openai", "label": "OpenAI", "base_url": "https://api.openai.com/v1", "wire_api": "auto", "auth_mode": "bearer", "auth_header": "Authorization", "auth_prefix": "Bearer", "models": ["gpt-5.5", "gpt-5.6", "gpt-4.1", "o3"]},
    {"id": "openrouter", "label": "OpenRouter", "base_url": "https://openrouter.ai/api/v1", "wire_api": "openai-chat", "auth_mode": "bearer", "auth_header": "Authorization", "auth_prefix": "Bearer", "models": []},
    {"id": "moonshot", "label": "Moonshot / Kimi", "base_url": "https://api.moonshot.cn/v1", "wire_api": "openai-chat", "auth_mode": "bearer", "auth_header": "Authorization", "auth_prefix": "Bearer", "models": ["kimi-k2-0711-preview", "moonshot-v1-128k"]},
    {"id": "zhipu", "label": "Zhipu GLM", "base_url": "https://open.bigmodel.cn/api/paas/v4", "wire_api": "openai-chat", "auth_mode": "bearer", "auth_header": "Authorization", "auth_prefix": "Bearer", "models": ["glm-4.5", "glm-4-plus"]},
    {"id": "qwen", "label": "Qwen / DashScope", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "wire_api": "openai-chat", "auth_mode": "bearer", "auth_header": "Authorization", "auth_prefix": "Bearer", "models": ["qwen-max", "qwen-plus", "qwen3-coder-plus"]},
    {"id": "siliconflow", "label": "SiliconFlow", "base_url": "https://api.siliconflow.cn/v1", "wire_api": "openai-chat", "auth_mode": "bearer", "auth_header": "Authorization", "auth_prefix": "Bearer", "models": []},
    {"id": "volcengine-ark", "label": "Volcengine Ark", "base_url": "https://ark.cn-beijing.volces.com/api/v3", "wire_api": "openai-chat", "auth_mode": "bearer", "auth_header": "Authorization", "auth_prefix": "Bearer", "models": []},
    {"id": "groq", "label": "Groq", "base_url": "https://api.groq.com/openai/v1", "wire_api": "openai-chat", "auth_mode": "bearer", "auth_header": "Authorization", "auth_prefix": "Bearer", "models": ["llama-3.3-70b-versatile"]},
    {"id": "together", "label": "Together AI", "base_url": "https://api.together.xyz/v1", "wire_api": "openai-chat", "auth_mode": "bearer", "auth_header": "Authorization", "auth_prefix": "Bearer", "models": []},
    {"id": "custom-openai", "label": "自定义 OpenAI 兼容中转站", "base_url": "", "wire_api": "auto", "auth_mode": "bearer", "auth_header": "Authorization", "auth_prefix": "Bearer", "models": []},
]


def provider_secret_root(sessions_root: str | Path) -> Path:
    return Path(sessions_root) / "_secrets" / "llm_providers"


def valid_provider_id(provider_id: str) -> bool:
    return bool(_PROVIDER_ID_RE.fullmatch(provider_id or ""))


def valid_endpoint(value: str) -> bool:
    try:
        parsed = urlparse(value)
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
    except ValueError:
        return False


def _clean_models(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        mid = str(item.get("id") if isinstance(item, dict) else item or "").strip()
        if mid and mid not in seen:
            seen.add(mid)
            out.append(mid)
    return out[:200]


def clean_llm_providers(value: Any, *, reject_invalid: bool = False) -> list[dict[str, Any]]:
    """Normalize public provider configs; never accepts/stores raw secret fields."""
    if value is None:
        return []
    if not isinstance(value, list):
        if reject_invalid:
            raise ValueError("llm_providers must be a list")
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            if reject_invalid:
                raise ValueError("llm provider must be an object")
            continue
        pid = str(item.get("id") or "").strip()
        if not valid_provider_id(pid):
            if reject_invalid:
                raise ValueError("llm provider requires a valid id")
            continue
        if pid.lower() in seen:
            if reject_invalid:
                raise ValueError(f"duplicate llm provider id: {pid}")
            continue
        base_url = str(item.get("base_url") or "").strip().rstrip("/")
        if base_url and not valid_endpoint(base_url):
            if reject_invalid:
                raise ValueError(f"llm provider {pid} has invalid base_url")
            continue
        wire_api = str(item.get("wire_api") or "auto").strip().lower()
        if wire_api not in _VALID_WIRE_APIS:
            wire_api = "auto"
        auth_mode = str(item.get("auth_mode") or "bearer").strip().lower()
        if auth_mode not in _VALID_AUTH_MODES:
            auth_mode = "bearer"
        auth_header = str(item.get("auth_header") or ("x-api-key" if auth_mode == "x-api-key" else "Authorization")).strip()
        if auth_mode == "custom" and not auth_header:
            if reject_invalid:
                raise ValueError(f"llm provider {pid} custom auth requires auth_header")
            auth_header = "Authorization"
        auth_prefix = str(item.get("auth_prefix") if item.get("auth_prefix") is not None else ("" if auth_mode == "x-api-key" else "Bearer")).strip()
        seen.add(pid.lower())
        out.append({
            "id": pid,
            "label": str(item.get("label") or item.get("name") or pid).strip() or pid,
            "kind": str(item.get("kind") or "openai-compatible").strip() or "openai-compatible",
            "base_url": base_url,
            "wire_api": wire_api,
            "auth_mode": auth_mode,
            "auth_header": auth_header,
            "auth_prefix": auth_prefix,
            "models": _clean_models(item.get("models")),
            "default_model": str(item.get("default_model") or (_clean_models(item.get("models")) or [""])[0]).strip(),
            "notes": str(item.get("notes") or "").strip(),
        })
    return out


@dataclass(frozen=True)
class ResolvedLLMProvider:
    provider_id: str
    label: str
    base_url: str
    api_key: str
    wire_api: str
    auth_mode: str
    auth_header: str
    auth_prefix: str
    models: list[str]
    has_api_key: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "label": self.label,
            "base_url": self.base_url,
            "wire_api": self.wire_api,
            "auth_mode": self.auth_mode,
            "auth_header": self.auth_header,
            "auth_prefix": self.auth_prefix,
            "models": list(self.models),
            "has_api_key": self.has_api_key,
        }


class LLMProviderSecretStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        chmod_private_dir(self.root)

    def secret_path(self, provider_id: str) -> Path:
        return self.root / provider_id / "API_KEY"

    def inspect(self, provider_id: str) -> dict[str, Any] | None:
        if not valid_provider_id(provider_id):
            return None
        base = self.root / provider_id
        if not base.exists() or not base.is_dir():
            return None
        p = base / "API_KEY"
        updated = updated_at(base)
        return {
            "provider_id": provider_id,
            "present": p.exists() and bool(self.read_secret(provider_id)),
            "updated_at": updated,
            "details": {"has_secret": p.exists()},
        }

    def list(self) -> list[dict[str, Any]]:
        if not self.root.exists():
            return []
        out = []
        for p in sorted(self.root.iterdir(), key=lambda x: x.name):
            if p.is_dir() and valid_provider_id(p.name):
                meta = self.inspect(p.name)
                if meta:
                    out.append(meta)
        return out

    def read_secret(self, provider_id: str) -> str:
        p = self.secret_path(provider_id)
        try:
            return p.read_text(encoding="utf-8").strip() if p.exists() else ""
        except OSError:
            return ""

    def upsert_secret(self, provider_id: str, secret: str) -> dict[str, Any]:
        provider_id = provider_id.strip()
        if not valid_provider_id(provider_id):
            raise ValueError("provider_id must be 1-64 chars: letters, digits, _, ., -")
        value = str(secret or "").strip()
        if not value:
            raise ValueError("API_KEY is required")
        base = self.root / provider_id
        base.mkdir(parents=True, exist_ok=True)
        chmod_private_dir(base)
        atomic_write(base / "API_KEY", value + "\n")
        meta = self.inspect(provider_id)
        assert meta is not None
        return meta

    def delete(self, provider_id: str) -> bool:
        if not valid_provider_id(provider_id):
            return False
        base = self.root / provider_id
        if not base.exists():
            return False
        shutil.rmtree(base)
        return True


def provider_secret_state(store: LLMProviderSecretStore, updates: list[dict[str, Any]] | None = None) -> dict[str, bool]:
    state = {str(row.get("provider_id")): bool(row.get("present")) for row in store.list()}
    for update in updates or []:
        if not isinstance(update, dict):
            continue
        pid = str(update.get("provider_id") or "").strip()
        action = str(update.get("action") or "").strip().lower()
        if not pid:
            continue
        if action == "remove":
            state[pid] = False
        elif action == "replace" and str(update.get("value") or "").strip():
            state[pid] = True
    return state


def resolve_llm_provider(
    provider_ref: str,
    providers: list[dict[str, Any]] | None,
    *,
    secret_store: LLMProviderSecretStore | None = None,
    api_key: str | None = None,
) -> ResolvedLLMProvider | None:
    ref = str(provider_ref or "").strip()
    if not ref:
        return None
    provider = next((p for p in clean_llm_providers(providers) if str(p.get("id")) == ref), None)
    if provider is None:
        return None
    key = str(api_key or "").strip()
    if not key and secret_store is not None:
        key = secret_store.read_secret(ref)
    return ResolvedLLMProvider(
        provider_id=ref,
        label=str(provider.get("label") or ref),
        base_url=str(provider.get("base_url") or "").strip().rstrip("/"),
        api_key=key,
        wire_api=str(provider.get("wire_api") or "auto"),
        auth_mode=str(provider.get("auth_mode") or "bearer"),
        auth_header=str(provider.get("auth_header") or "Authorization"),
        auth_prefix=str(provider.get("auth_prefix") if provider.get("auth_prefix") is not None else "Bearer"),
        models=list(provider.get("models") or []),
        has_api_key=bool(key),
    )


def public_provider_secrets(store: LLMProviderSecretStore) -> list[dict[str, Any]]:
    return store.list()


def probe_llm_provider(provider: Mapping[str, Any], *, api_key: str = "", model: str = "", validate_model: bool = False) -> dict[str, Any]:
    p = clean_llm_providers([dict(provider)], reject_invalid=True)[0]
    base_url = str(p.get("base_url") or "").strip()
    if not base_url:
        return {"ok": False, "detail": "Base URL 不能为空。", "error_layer": "base_url", "models": []}
    return probe_endpoint(
        {
            "base_url": normalize_base_url(base_url),
            "model": model or str(p.get("default_model") or ""),
            "wire_api": p.get("wire_api") or "auto",
            "auth_mode": p.get("auth_mode") or "bearer",
            "auth_header": p.get("auth_header") or "Authorization",
            "auth_prefix": p.get("auth_prefix") if p.get("auth_prefix") is not None else "Bearer",
        },
        api_key=api_key,
        validate_model=validate_model,
    )
