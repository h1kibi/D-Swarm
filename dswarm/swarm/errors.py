"""Swarm coordination errors."""

from __future__ import annotations


class WorkerSpawnRejected(RuntimeError):
    """A worker spawn was rejected for a recoverable reason before budget was spent."""
