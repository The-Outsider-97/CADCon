"""CADCon adapters for the three SLAI agents it integrates. Each adapter builds a documented ``perform_task`` request from *measured* data,
validates the agent's answer and returns an :class:`AgentResult`. Nothing here produces geometry, edits a model or invents a confidence.

* **reasoning** - explains measured diagnoses and analysis findings in words (advisory, ``ai_assisted``).
* **verification** - model-checks a declared postcondition over a two-state abstraction of a repair (before/after counts that CADCon
  measured). The ``verified`` result is about that abstraction and the declared invariant; it does not verify the geometry itself.
* **spatial** - rigid registration of two point sets (``align_point_clouds``); the transform and RMSE are the agent's numerical estimate
  (``estimated``), checked here for shape/finite values only.
"""
from __future__ import annotations

import math
from typing import Any, Sequence

try:
    from ...utils import cadcon_helpers as helpers
except ImportError:
    from utils import cadcon_helpers as helpers

from .runtime import AgentResult, AgentRuntime, COMPLETED

REASONING_LIMITATION = "Agent interpretation is advisory; only deterministic geometry metrics are authoritative"
_COUNT_KEYS = ("boundary_edges", "nonmanifold_edges", "inconsistent_winding_edges")


def _counts(block: Any) -> dict[str, Any]:
    block = block if isinstance(block, dict) else {}
    return {key: block.get(key) for key in _COUNT_KEYS}


def _text_of(response: Any) -> str:
    """Reasoning responses vary in shape; accept only a non-empty text from the documented keys."""
    if not isinstance(response, dict) or response.get("status") not in {"ok", "success"}:
        raise ValueError("not a successful structured response")
    raw = response.get("result", response.get("output"))
    if isinstance(raw, dict):
        raw = raw.get("explanation") or raw.get("summary") or raw.get("result")
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("no usable grounded explanation")
    return raw.strip()[:2400]


# ----------------------------------------------------------------------------------------------------------------- reasoning
def repair_advice(rt: AgentRuntime, diagnosis: dict[str, Any], candidates: Sequence[dict[str, Any]],
                  *, timeout: float | None = None, token: helpers.CancellationToken | None = None) -> AgentResult:
    grounded = {"source": _counts(diagnosis.get("source")), "reconstructed": _counts(diagnosis.get("reconstructed")),
                "candidates": [{"strategy": c.get("strategy"), "status": (c.get("validation") or {}).get("status"),
                                "added_faces": c.get("added_faces")} for c in candidates]}
    request = {"task_type": "reason",
               "problem": "Explain the observed CAD mesh topology and discuss the verified repair candidates. "
                          "Distinguish topology closure from solid validity. Do not invent dimensions or checks.",
               "context": {"geometry_evidence": grounded}}

    def validate(response: Any):
        return {"interpretation": _text_of(response), "grounded_in": grounded}, None, [REASONING_LIMITATION]

    return rt.call("reasoning", "repair_advice", "explain_repair_candidates", request, validate, timeout=timeout, token=token)


def analysis_interpretation(rt: AgentRuntime, findings: Sequence[dict[str, Any]], summary: dict[str, Any],
                            *, timeout: float | None = None, token: helpers.CancellationToken | None = None) -> AgentResult:
    grounded = {"summary": {k: summary.get(k) for k in sorted(summary) if isinstance(summary.get(k), (int, float, str, bool))},
                "findings": [{"code": f.get("code"), "severity": f.get("severity"), "message": f.get("message")} for f in list(findings)[:20]]}
    request = {"task_type": "reason",
               "problem": "Explain what these measured CAD analysis findings mean for the model and what a careful engineer should check next. "
                          "Use only the supplied measurements; do not invent dimensions, tolerances or standards.",
               "context": {"geometry_evidence": grounded}}

    def validate(response: Any):
        return {"interpretation": _text_of(response), "grounded_in": grounded}, None, [REASONING_LIMITATION]

    return rt.call("reasoning", "analysis_interpretation", "interpret_analysis", request, validate, timeout=timeout, token=token)


# --------------------------------------------------------------------------------------------------------------- verification
POSTCONDITIONS = (
    ("boundary_edges_not_increased", "boundary_edges", "Open boundary edges do not increase"),
    ("nonmanifold_edges_not_increased", "nonmanifold_edges", "Non-manifold edges do not increase"),
    ("winding_inconsistency_not_increased", "inconsistent_winding_edges", "Inconsistently wound edges do not increase"),
)


def candidate_postconditions(rt: AgentRuntime, before: dict[str, Any], after: dict[str, Any], *, label: str = "repair",
                             timeout: float | None = None, token: helpers.CancellationToken | None = None) -> list[AgentResult]:
    """One model-checking run per declared postcondition over the abstraction ``before --repair--> after`` (measured counts)."""
    before_counts, after_counts = _counts(before), _counts(after)
    results: list[AgentResult] = []
    try:
        from src.agents.verification import Invariant, ResourceBounds, Transition, TransitionSystem
    except Exception as exc:  # noqa: BLE001 - SLAI verification package not importable here
        stub = AgentResult(agent="verification", operation="postcondition", workflow="candidate_postconditions", status="unavailable",
                           reason=f"The SLAI verification package could not be imported ({type(exc).__name__})")
        return [rt._record(stub)]
    for name, key, text in POSTCONDITIONS:
        b, a = before_counts.get(key), after_counts.get(key)
        if not isinstance(b, int) or not isinstance(a, int) or isinstance(b, bool) or isinstance(a, bool):
            skipped = AgentResult(agent="verification", operation=name, workflow="candidate_postconditions", status="unavailable",
                                  reason=f"{key} was not measured for this candidate")
            results.append(rt._record(skipped))
            continue
        s0, s1 = ("before", ((key, b),)), ("after", ((key, a),))
        system = TransitionSystem(states=(s0, s1), initial_states=(s0,), transitions=(Transition(s0, s1, label),))
        invariant = Invariant(name, lambda s, _k=key, _b=b: s[0] != "after" or dict(s[1])[_k] <= _b, text)
        bounds = ResourceBounds(max_states=16, max_transitions=16, timeout_seconds=float(timeout or rt.config.agent_timeout("verification")))
        request = {"operation": "invariant", "artifact": system, "formal_property": invariant, "bounds": bounds}

        def validate(response: Any, _name=name, _text=text, _b=b, _a=a):
            if not isinstance(response, dict) or response.get("execution_success") is not True:
                raise ValueError("verification run did not complete")
            status = response.get("verification_status")
            if status not in {"verified", "refuted"}:
                raise ValueError(f"inconclusive verification status {status!r}")
            formal = response.get("formal_result") or {}
            prov = formal.get("provenance") if isinstance(formal, dict) else None
            return ({"property": _name, "statement": _text, "before": _b, "after": _a, "verification_status": status,
                     "method": (prov or {}).get("method") if isinstance(prov, dict) else None,
                     "summary": formal.get("summary") if isinstance(formal, dict) else None,
                     "scope": "Two-state abstraction built from CADCon's measured counts; the geometry itself is not model-checked"},
                    status, ["Checks the declared postcondition over measured counts only; it is not proof of solid validity"])

        outcome = rt.call("verification", "candidate_postconditions", name, request, validate, timeout=timeout, token=token)
        if outcome.status == COMPLETED:  # a check that ran is "verified" evidence whichever way it came out; the claim says which
            outcome.evidence = [helpers.evidence(helpers.VERIFIED, "src.agents.verification_agent.VerificationAgent",
                                                 f"{text}: {outcome.assessment} by model checking over the before/after abstraction",
                                                 operation_id=outcome.operation_id)]
        results.append(outcome)
    return results


# ------------------------------------------------------------------------------------------------------------------ spatial
def register_point_sets(rt: AgentRuntime, source: Sequence[Sequence[float]], target: Sequence[Sequence[float]], *,
                        max_iterations: int = 50, tolerance: float = 1e-6, timeout: float | None = None,
                        token: helpers.CancellationToken | None = None) -> AgentResult:
    """Rigidly register ``source`` onto ``target`` with the Spatial Agent. Both sets must be equal-size 3D samples."""
    request = {"operation": "align_point_clouds", "source": [list(map(float, p)) for p in source],
               "target": [list(map(float, p)) for p in target], "max_iterations": int(max_iterations), "tolerance": float(tolerance)}

    def validate(response: Any):
        if not isinstance(response, dict) or response.get("status") not in {"ok", "success"}:
            raise ValueError("registration did not return a successful structured response")
        result = response.get("result")
        if not isinstance(result, dict):
            raise ValueError("registration returned no result")
        rmse = result.get("rmse")
        if not isinstance(rmse, (int, float)) or isinstance(rmse, bool) or not math.isfinite(float(rmse)):
            raise ValueError("registration RMSE is not a finite number")
        transform = result.get("transform")
        if transform is None:
            raise ValueError("registration returned no transform")
        return ({"transform": transform, "rmse": float(rmse), "iterations": result.get("iterations"), "converged": result.get("converged")},
                "converged" if result.get("converged") else "not_converged",
                ["Rigid registration estimate from sampled surface points; it is not a CAD-level feature match"])

    return rt.call("spatial", "compare_registration", "align_point_clouds", request, validate, timeout=timeout, token=token,
                   evidence_kind=helpers.ESTIMATED, claim="Rigid registration of sampled surface points by the SLAI Spatial Agent")
