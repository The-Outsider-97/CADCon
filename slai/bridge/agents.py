"""Backward-compatible facade over the capability-driven SLAI integration layer (``runtime`` + ``adapters``).

``mesh_repair_advice`` keeps its original signature and result shape; new callers use :func:`get_runtime` and the adapters directly.
No synthetic stand-in agents, geometry generation or direct model edits.
"""
from __future__ import annotations

import threading
from typing import Any

try:
    from ...utils import cadcon_helpers as helpers
except ImportError:
    from utils import cadcon_helpers as helpers

from . import adapters
from .runtime import AgentResult, AgentRuntime, COMPLETED, DISABLED

_runtime: AgentRuntime | None = None
_lock = threading.Lock()


def get_runtime() -> AgentRuntime:
    global _runtime
    with _lock:
        if _runtime is None:
            _runtime = AgentRuntime()
        return _runtime


def shutdown_runtime() -> None:
    global _runtime
    with _lock:
        runtime, _runtime = _runtime, None
    if runtime is not None:
        runtime.shutdown()


def mesh_repair_advice(diagnosis: dict[str, Any], candidates: list[dict[str, Any]],
                       *, enabled: bool = True, timeout_seconds: float | None = None, runtime: AgentRuntime | None = None) -> dict[str, Any]:
    """Legacy shape: ``{status: disabled|unavailable|completed, ...}``; the full normalised record is under ``record``."""
    if not enabled:
        return {"status": "disabled", "reason": "SLAI agent analysis was disabled for this operation"}
    rt = runtime or get_runtime()
    outcome = adapters.repair_advice(rt, diagnosis, candidates, timeout=timeout_seconds)
    if outcome.status == COMPLETED:
        return {"status": "completed", "agent": "src.agents.reasoning_agent.ReasoningAgent",
                "interpretation": outcome.result["interpretation"], "evidence": outcome.result["grounded_in"],
                "evidence_records": outcome.evidence, "limitations": adapters.REASONING_LIMITATION, "record": outcome.to_dict()}
    return {"status": "disabled" if outcome.status == DISABLED else "unavailable", "reason": outcome.reason, "record": outcome.to_dict()}


__all__ = ["mesh_repair_advice", "get_runtime", "shutdown_runtime", "AgentResult", "AgentRuntime"]
