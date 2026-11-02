"""Optional, evidence-bounded SLAI Reasoning Agent advice for CADCon repairs.

Uses SLAI's AgentFactory and documented ReasoningAgent.perform_task contract.
No synthetic stand-in agents, geometry generation or direct model edits.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError
from typing import Any


def mesh_repair_advice(diagnosis: dict[str, Any], candidates: list[dict[str, Any]],
                       *, enabled: bool = True, timeout_seconds: float = 8.0) -> dict[str, Any]:
    if not enabled:
        return {"status": "disabled", "reason": "SLAI agent analysis was disabled for this operation"}
    evidence = {
        "source": {key: diagnosis.get("source", {}).get(key) for key in
                   ("boundary_edges", "nonmanifold_edges", "inconsistent_winding_edges")},
        "reconstructed": {key: diagnosis.get("reconstructed", {}).get(key) for key in
                          ("boundary_edges", "nonmanifold_edges", "inconsistent_winding_edges")},
        "candidates": [{"strategy": item.get("strategy"), "status": item.get("validation", {}).get("status"),
                        "added_faces": item.get("added_faces")} for item in candidates],
    }

    def invoke() -> Any:
        from src.agents.agent_factory import AgentFactory
        agent = AgentFactory().get_agent("reasoning")
        if not callable(getattr(agent, "perform_task", None)):
            raise RuntimeError("Configured Reasoning Agent has no perform_task interface")
        return agent.perform_task({
            "task_type": "reason",
            "problem": "Explain the observed CAD mesh topology and discuss the verified repair candidates. "
                       "Distinguish topology closure from solid validity. Do not invent dimensions or checks.",
            "context": {"geometry_evidence": evidence},
        })

    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cadcon-slai-reasoning")
    future = pool.submit(invoke)
    try:
        response = future.result(timeout=timeout_seconds)
        if not isinstance(response, dict) or response.get("status") not in {"ok", "success"}:
            return {"status": "unavailable", "reason": "SLAI Reasoning Agent did not return a successful structured response"}
        raw = response.get("result", response.get("output"))
        if isinstance(raw, dict):
            raw = raw.get("explanation") or raw.get("summary") or raw.get("result")
        if not isinstance(raw, str) or not raw.strip():
            return {"status": "unavailable", "reason": "Reasoning Agent returned no usable grounded explanation"}
        return {"status": "completed", "agent": "src.agents.reasoning_agent.ReasoningAgent",
                "interpretation": raw[:2400], "evidence": evidence,
                "limitations": "Agent interpretation is advisory; only deterministic geometry metrics are authoritative"}
    except TimeoutError:
        future.cancel()
        return {"status": "unavailable", "reason": f"SLAI Reasoning Agent exceeded the {timeout_seconds:g}-second budget"}
    except Exception as exc:
        return {"status": "unavailable", "reason": f"SLAI Reasoning Agent unavailable ({type(exc).__name__}); deterministic repair remains available"}
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
