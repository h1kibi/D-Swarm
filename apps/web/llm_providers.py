"""Backward-compatible re-export of the core LLM provider registry.

The provider domain lives in ``dswarm.solver.llm_providers`` so both the web
layer and the swarm core can consume it without inverting the dependency
direction.  Existing ``apps.web.llm_providers`` imports keep working.
"""

from dswarm.solver.llm_providers import (
    DEFAULT_PROVIDER_TEMPLATES,
    LLMProviderSecretStore,
    ResolvedLLMProvider,
    clean_llm_providers,
    probe_llm_provider,
    provider_secret_root,
    provider_secret_state,
    public_provider_secrets,
    resolve_llm_provider,
    valid_endpoint,
    valid_provider_id,
)

__all__ = [
    "DEFAULT_PROVIDER_TEMPLATES",
    "LLMProviderSecretStore",
    "ResolvedLLMProvider",
    "clean_llm_providers",
    "probe_llm_provider",
    "provider_secret_root",
    "provider_secret_state",
    "public_provider_secrets",
    "resolve_llm_provider",
    "valid_endpoint",
    "valid_provider_id",
]
