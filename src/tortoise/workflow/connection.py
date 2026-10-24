"""Connecting to the Temporal dev server (address from the environment)."""

from __future__ import annotations

from temporalio.client import Client

from ..config import env_str

DEFAULT_ADDRESS = "localhost:7233"

# The one task queue every Tortoise worker and workflow share.
TASK_QUEUE = "tortoise"


async def connect() -> Client:
    """Connect to the local Temporal dev server (Week 1's ``--db-filename`` server)."""
    address = env_str("TEMPORAL_ADDRESS", DEFAULT_ADDRESS) or DEFAULT_ADDRESS
    namespace = env_str("TEMPORAL_NAMESPACE", "default") or "default"
    return await Client.connect(address, namespace=namespace)
