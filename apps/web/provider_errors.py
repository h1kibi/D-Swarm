"""Backward-compatible web import for provider/runtime diagnostics."""

from dswarm.core.provider_errors import (
    ProviderErrorAggregator,
    ProviderErrorDiagnostic,
    classify_provider_error,
)

__all__ = [
    "ProviderErrorAggregator",
    "ProviderErrorDiagnostic",
    "classify_provider_error",
]
