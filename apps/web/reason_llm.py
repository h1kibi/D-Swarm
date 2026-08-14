"""ReasonSwarm host-side LLM endpoint resolution and probes.

This module intentionally mirrors the UI settings contract without exposing raw
secrets.  The planner/titler are host-side OpenAI-compatible calls; they may use
.env, or a persisted credential account created for Worker endpoints.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from apps.web.worker_config import DEFAULT_DEEPSEEK_BASE_URL
from apps.web.llm_providers import (
    LLMProviderSecretStore,
    provider_secret_root,
    resolve_llm_provider,
    valid_endpoint,
)
from dswarm.core.llm import classify_llm_exception
from dswarm.solver.credential_accounts import CredentialAccountStore, account_store_root
from dswarm.solver.worker_profiles import profile_label, profile_ref

DEFAULT_REASON_ACCOUNT = "pi-main"
_VALID_SOURCES = {"auto", "env", "account"}


def _norm_url(value: str) -> str:
    return (value or "").strip().rstrip("/")


def _is_default_deepseek(value: str) -> bool:
    return _norm_url(value) == _norm_url(DEFAULT_DEEPSEEK_BASE_URL)


def base_url_host(value: str) -> str:
    try:
        return urlparse(value).netloc or value
    except ValueError:
        return value


_valid_endpoint = valid_endpoint


_profile_label = profile_label
_profile_ref = profile_ref


def _candidate_account_ids(worker_profiles: list[dict[str, Any]]) -> list[str]:
    """Prefer system worker, then enabled workers, then any profile account."""
    ordered: list[dict[str, Any]] = []
    system = [
        p for p in worker_profiles
        if isinstance(p, dict) and _profile_label(p).lower() == "pi-worker"
    ]
    enabled = [
        p for p in worker_profiles
        if isinstance(p, dict) and bool(p.get("enabled", True))
           and _profile_label(p).lower() != "pi-worker"
    ]
    rest = [p for p in worker_profiles if isinstance(p, dict)]
    ordered.extend(system)
    ordered.extend(enabled)
    ordered.extend(rest)
    out: list[str] = []
    seen: set[str] = set()
    for profile in ordered:
        account_id = str(profile.get("credential_account") or "").strip()
        if account_id and account_id not in seen:
            seen.add(account_id)
            out.append(account_id)
    if DEFAULT_REASON_ACCOUNT not in seen:
        out.append(DEFAULT_REASON_ACCOUNT)
    return out


def _inspect_account(store: CredentialAccountStore | None, account_id: str) -> Any:
    if store is None or not account_id:
        return None
    try:
        return store.inspect(account_id)
    except Exception:  # noqa: BLE001 - settings diagnostics must not crash callers
        return None


def _account_material(acct: Any) -> tuple[str, str, str]:
    if acct is None or not getattr(acct, "present", False):
        return "", "", ""
    details = getattr(acct, "details", None) or {}
    key = str(details.get("secret_value") or "").strip()
    base = _norm_url(str(details.get("base_url_value") or ""))
    return key, base, str(getattr(acct, "account_id", "") or "")


def resolve_reason_llm_endpoint(
    *,
    sessions_root: str | Path | None,
    worker_profiles: list[dict[str, Any]] | None,
    profile: Mapping[str, Any] | None,
    llm_providers: list[dict[str, Any]] | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Resolve actual endpoint/key for Planner/Titler.

    ``base_url`` remains visible config.  The key never crosses API boundaries.
    ``credential_source=auto`` keeps backwards compatibility: env wins when
    present; otherwise the configured account (or migrated system Worker account)
    is used.  When the profile still carries the DeepSeek default but the account
    is a custom relay, the account BASE_URL wins; this fixes the observed desktop
    failure where Reason tried DeepSeek with an empty key while Workers used a
    relay account.
    """
    env = os.environ if env is None else env
    worker_profiles = worker_profiles or []
    profile = profile or {}
    source = str(profile.get("credential_source") or "auto").strip().lower() or "auto"
    if source not in _VALID_SOURCES:
        source = "auto"
    configured_base = _norm_url(str(profile.get("base_url") or "")) or DEFAULT_DEEPSEEK_BASE_URL
    configured_account = str(profile.get("credential_account") or "").strip()
    env_key = str(env.get("DSWARM_DEEPSEEK_API_KEY") or env.get("DEEPSEEK_API_KEY") or "").strip()
    env_base = _norm_url(str(env.get("DSWARM_DEEPSEEK_BASE_URL") or ""))

    store = None
    if sessions_root is not None:
        store = CredentialAccountStore(account_store_root(sessions_root))


    provider_ref = str(profile.get("provider_ref") or "").strip()
    if provider_ref:
        provider_store = LLMProviderSecretStore(provider_secret_root(sessions_root)) if sessions_root is not None else None
        resolved_provider = resolve_llm_provider(provider_ref, llm_providers or [], secret_store=provider_store)
        if resolved_provider is not None:
            return {
                "api_key": resolved_provider.api_key,
                "base_url": _norm_url(resolved_provider.base_url),
                "model": str(profile.get("model") or "").strip(),
                "credential_source": "provider",
                "credential_account": provider_ref,
                "provider_ref": provider_ref,
                "base_url_source": "provider",
                "base_url_host": base_url_host(resolved_provider.base_url),
                "has_api_key": bool(resolved_provider.api_key),
                "configured_base_url": resolved_provider.base_url,
                "account_base_url": "",
                "wire_api": resolved_provider.wire_api,
                "auth_mode": resolved_provider.auth_mode,
                "auth_header": resolved_provider.auth_header,
                "auth_prefix": resolved_provider.auth_prefix,
            }

    account_ids = []
    if configured_account:
        account_ids.append(configured_account)
    account_ids.extend(_candidate_account_ids(worker_profiles))
    deduped: list[str] = []
    seen: set[str] = set()
    for aid in account_ids:
        if aid and aid not in seen:
            seen.add(aid)
            deduped.append(aid)

    account_key = ""
    account_base = ""
    effective_account = configured_account or (deduped[0] if deduped else "")
    for aid in deduped:
        key, base, found_id = _account_material(_inspect_account(store, aid))
        if key:
            account_key, account_base, effective_account = key, base, found_id or aid
            if configured_account and aid == configured_account:
                break
            if not configured_account:
                break

    credential_source = source
    api_key = ""
    if source == "env":
        api_key = env_key
        credential_source = "env"
    elif source == "account":
        api_key = account_key
        credential_source = "account"
    else:
        if env_key:
            api_key = env_key
            credential_source = "env"
        else:
            api_key = account_key
            credential_source = "account" if account_key else "auto"

    if credential_source == "env":
        base_url = configured_base or env_base or DEFAULT_DEEPSEEK_BASE_URL
        base_source = "profile" if configured_base else "env"
        if _is_default_deepseek(base_url) and env_base:
            base_url = env_base
            base_source = "env"
    else:
        # Account relay BASE_URL should override the baked DeepSeek default, but
        # not an operator-entered non-default Planner URL.
        if account_base and (not configured_base or _is_default_deepseek(configured_base)):
            base_url = account_base
            base_source = "account"
        else:
            base_url = configured_base or account_base or DEFAULT_DEEPSEEK_BASE_URL
            base_source = "profile" if configured_base else "account"

    return {
        "api_key": api_key,
        "base_url": _norm_url(base_url),
        "model": str(profile.get("model") or "").strip(),
        "credential_source": credential_source,
        "credential_account": effective_account,
        "base_url_source": base_source,
        "base_url_host": base_url_host(base_url),
        "has_api_key": bool(api_key),
        "configured_base_url": configured_base,
        "account_base_url": account_base,
        "auth_mode": str(profile.get("auth_mode") or "bearer").strip().lower(),
        "auth_header": str(profile.get("auth_header") or "Authorization").strip(),
        "auth_prefix": str(
            profile.get("auth_prefix")
            if profile.get("auth_prefix") is not None
            else "Bearer"
        ).strip(),
    }

async def probe_reason_llm_endpoint(
    *,
    which: str,
    base_url: str | None,
    model: str | None,
    sessions_root: str | Path | None = None,
    worker_profiles: list[dict[str, Any]] | None = None,
    credential_account: str | None = None,
    credential_source: str | None = None,
    wire_api: str | None = None,
    provider_ref: str | None = None,
    llm_providers: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    from dswarm.core.llm import LLMClient

    which = (which or "planner").strip() or "planner"
    model = (model or "").strip()
    if not model:
        return {"ok": False, "code": "missing_model", "detail": "model 不能为空", "model": "", "layers": []}
    profile = {
        "base_url": (base_url or "").strip(),
        "model": model,
        "credential_account": (credential_account or "").strip(),
        "credential_source": (credential_source or "auto").strip() or "auto",
        "wire_api": (wire_api or "auto").strip() or "auto",
        "provider_ref": (provider_ref or "").strip(),
    }
    resolved = resolve_reason_llm_endpoint(
        sessions_root=sessions_root,
        worker_profiles=worker_profiles or [],
        profile=profile,
        llm_providers=llm_providers or [],
    )
    layers: list[dict[str, Any]] = []
    endpoint = str(resolved["base_url"] or "")
    endpoint_ok = _valid_endpoint(endpoint)
    layers.append({"name": "base_url", "ok": endpoint_ok, "detail": base_url_host(endpoint)})
    if not endpoint_ok:
        return {
            "ok": False, "code": "invalid_base_url", "detail": "Base URL 必须是 http(s) URL",
            "model": model, "base_url": endpoint, "base_url_host": base_url_host(endpoint),
            "credential_source": resolved["credential_source"], "credential_account": resolved["credential_account"],
            "layers": layers,
        }
    auth_ok = bool(resolved.get("api_key"))
    layers.append({
        "name": "auth", "ok": auth_ok,
        "detail": str(resolved.get("credential_account") or resolved.get("credential_source") or ""),
    })
    if not auth_ok:
        return {
            "ok": False, "code": "missing_api_key",
            "detail": "Planner/Titler 缺少 API 密钥：请选择凭据账户或设置 DSWARM_DEEPSEEK_API_KEY。",
            "model": model, "base_url": endpoint, "base_url_host": base_url_host(endpoint),
            "credential_source": resolved["credential_source"], "credential_account": resolved["credential_account"],
            "layers": layers,
        }
    layers.append({"name": "models", "ok": True, "attempted": False, "detail": "/models optional"})
    resolved_auth_prefix = resolved.get("auth_prefix")
    client = LLMClient(
        api_key=str(resolved["api_key"]),
        base_url=endpoint,
        auth_mode=str(resolved.get("auth_mode") or "bearer"),
        auth_header=str(resolved.get("auth_header") or "Authorization"),
        auth_prefix="Bearer" if resolved_auth_prefix is None else str(resolved_auth_prefix),
    )
    try:
        resp = await client.chat(
            model=model,
            messages=[{"role": "user", "content": "ping"}],
            temperature=0.0,
            stream=False,
        )
        fr = getattr(resp, "finish_reason", "") or ""
        if fr == "error":
            layers.append({"name": "chat", "ok": False, "detail": "finish_reason=error"})
            return {
                "ok": False, "code": "finish_reason_error", "detail": "endpoint 返回 error finish_reason",
                "model": model, "base_url": endpoint, "base_url_host": base_url_host(endpoint),
                "credential_source": resolved["credential_source"], "credential_account": resolved["credential_account"],
                "layers": layers,
            }
        layers.append({"name": "chat", "ok": True, "detail": fr or "ok"})
        return {
            "ok": True, "code": "ok", "detail": "端点可达，凭据有效，模型可调用",
            "model": model, "base_url": endpoint, "base_url_host": base_url_host(endpoint),
            "credential_source": resolved["credential_source"], "credential_account": resolved["credential_account"],
            "detected_wire_api": "openai-chat" if (wire_api or "auto") in {"auto", "openai-chat", ""} else wire_api,
            "layers": layers,
        }
    except Exception as exc:  # noqa: BLE001
        diag = classify_llm_exception(exc)
        layers.append({"name": "chat", "ok": False, "status": diag.get("status"), "detail": diag["detail"]})
        return {
            "ok": False, "code": diag["code"], "detail": diag["detail"],
            "model": model, "base_url": endpoint, "base_url_host": base_url_host(endpoint),
            "credential_source": resolved["credential_source"], "credential_account": resolved["credential_account"],
            "layers": layers,
        }
    finally:
        try:
            await client.aclose()
        except Exception:  # noqa: BLE001
            pass
