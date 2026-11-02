"""SLAI agent integration: lifecycle, isolation, timeouts, routing, evidence classes. Fakes stand in for the *factory* only so failure
modes can be forced; the final tests run the real Spatial and Verification agents when they can be imported."""
from __future__ import annotations

import threading
import time

import pytest

from slai.bridge import adapters, agents as facade
from slai.bridge.runtime import AgentRuntime, COMPLETED, DISABLED, FAILED, INVALID, TIMEOUT, UNAVAILABLE
from utils import cadcon_helpers as helpers, config_loader as cfg


class FakeAgent:
    def __init__(self, behaviour):
        self.behaviour, self.calls = behaviour, 0

    def perform_task(self, request):
        self.calls += 1
        return self.behaviour(request)

    def capabilities(self):
        return {"operations": ["x"]}


class FakeFactory:
    """Stands in for SLAI's AgentFactory (the single place that creates agents)."""
    instances: list["FakeFactory"] = []
    behaviours: dict = {}
    load_error: Exception | None = None
    load_delay = 0.0

    def __init__(self):
        self.agents, self.released, self.shut = {}, [], False
        FakeFactory.instances.append(self)

    def get_agent(self, key):
        if FakeFactory.load_delay:
            time.sleep(FakeFactory.load_delay)
        if FakeFactory.load_error:
            raise FakeFactory.load_error
        return self.agents.setdefault(key, FakeAgent(FakeFactory.behaviours[key]))

    def release(self, key):
        self.released.append(key)

    def shutdown(self):
        self.shut = True


@pytest.fixture
def fake(monkeypatch):
    import src.agents.agent_factory as module
    FakeFactory.instances, FakeFactory.behaviours, FakeFactory.load_error, FakeFactory.load_delay = [], {}, None, 0.0
    monkeypatch.setattr(module, "AgentFactory", FakeFactory)
    return FakeFactory


def runtime(**overrides):
    base = {"agents.default_timeout_seconds": 1.0, "agents.failure_cooldown_seconds": 30.0}
    return AgentRuntime(cfg.load_config(overrides={**base, **overrides}, environ={}, force_reload=True))


def ok(request):
    return {"status": "ok", "result": {"explanation": "The boundary loops are consistent with a missing face."}}


def reason(rt, **kw):
    return adapters.repair_advice(rt, {"source": {"boundary_edges": 4}, "reconstructed": {"boundary_edges": 4}}, [{"strategy": "hole_fill"}], **kw)


def test_completed_call_is_tagged_ai_assisted_and_grounded(fake):
    fake.behaviours["reasoning"] = ok
    rt = runtime()
    result = reason(rt)
    assert result.status == COMPLETED and result.ok
    assert result.evidence[0]["kind"] == helpers.AI_ASSISTED
    assert result.result["grounded_in"]["source"]["boundary_edges"] == 4
    assert result.confidence is None, "a confidence must never be invented"
    assert helpers.AI_ASSISTED in str(result.to_dict())
    rt.shutdown()


def test_master_switch_agent_switch_and_route_all_disable_without_loading(fake):
    fake.behaviours["reasoning"] = ok
    assert reason(runtime(**{"agents.enabled": False})).status == DISABLED
    assert reason(runtime(**{"agents.integrations": {"reasoning": {"enabled": False}}})).status == DISABLED
    routed_out = runtime(**{"agents.workflows": {"repair_advice": []}})
    assert reason(routed_out).status == DISABLED
    assert not fake.instances, "a disabled agent must never be constructed"


def test_failure_does_not_propagate_and_triggers_cooldown(fake):
    def boom(_):
        raise RuntimeError("model exploded")
    fake.behaviours["reasoning"] = boom
    rt = runtime()
    first = reason(rt)
    assert first.status == FAILED and "RuntimeError" in first.reason and "exploded" not in first.reason
    agent = fake.instances[0].agents["reasoning"]
    second = reason(rt)
    assert second.status == UNAVAILABLE and "resting" in second.reason
    assert agent.calls == 1, "a failing agent is not hammered during its cooldown"
    rt.reset_cooldown()
    assert reason(rt).status == FAILED


def test_timeout_is_bounded_counted_and_late_answer_discarded(fake):
    release = threading.Event()
    fake.behaviours["reasoning"] = lambda request: (release.wait(5), ok(request))[1]
    rt = runtime(**{"agents.default_timeout_seconds": 0.5})
    started = time.perf_counter()
    result = reason(rt)
    release.set()
    assert result.status == TIMEOUT and time.perf_counter() - started < 3.0
    assert rt.metrics()["abandoned_calls"] == 1
    assert rt.health("reasoning")["metrics"]["timeouts"] == 1
    rt.shutdown()


def test_invalid_response_is_rejected_not_repaired(fake):
    fake.behaviours["reasoning"] = lambda request: {"status": "ok", "result": ""}
    assert reason(runtime()).status == INVALID
    fake.behaviours["reasoning"] = lambda request: "free text"
    assert reason(runtime()).status == INVALID


def test_load_failure_reports_cause_and_keeps_cadcon_usable(fake):
    fake.load_error = RuntimeError("torch is not installed")
    result = reason(runtime())
    assert result.status == UNAVAILABLE and "RuntimeError" in result.reason and "deterministic" in result.reason


def test_first_use_loading_is_bounded_and_finishes_in_the_background(fake):
    fake.behaviours["reasoning"] = ok
    fake.load_delay = 1.6
    rt = runtime(**{"agents.load_timeout_seconds": 1.0})
    first = reason(rt)
    assert first.status == UNAVAILABLE and "still loading" in first.reason
    time.sleep(1.2)                       # the load completes without blocking any request
    assert reason(rt).status == COMPLETED
    rt.shutdown()


def test_lifecycle_loads_lazily_once_and_releases_through_the_factory(fake):
    fake.behaviours["reasoning"] = ok
    rt = runtime()
    assert rt.health("reasoning")["state"] == "not_loaded" and not fake.instances
    reason(rt); reason(rt)
    factory = fake.instances[0]
    assert len(fake.instances) == 1 and factory.agents["reasoning"].calls == 2
    assert rt.health("reasoning")["state"] == "ready"
    rt.shutdown()
    assert factory.released == ["reasoning"] and factory.shut
    rt.shutdown()   # idempotent


def test_replaced_factory_class_is_picked_up_instead_of_stale_cache(fake, monkeypatch):
    fake.behaviours["reasoning"] = ok
    rt = runtime()
    reason(rt)
    class Other(FakeFactory):
        pass
    import src.agents.agent_factory as module
    monkeypatch.setattr(module, "AgentFactory", Other)
    reason(rt)
    assert len(fake.instances) == 2


def test_saturation_is_refused_rather_than_queued_forever(fake):
    gate = threading.Event()
    fake.behaviours["reasoning"] = lambda request: (gate.wait(5), ok(request))[1]
    rt = runtime(**{"agents.max_workers": 1})
    outcomes = []
    for _ in range(6):
        outcomes.append(reason(rt, timeout=0.1).status)
        rt.reset_cooldown()
    gate.set()
    assert TIMEOUT in outcomes and "saturated" in outcomes
    rt.shutdown()


def test_cancelled_token_prevents_the_call(fake):
    fake.behaviours["reasoning"] = ok
    token = helpers.CancellationToken(); token.cancel()
    assert reason(runtime(), token=token).status == UNAVAILABLE and not fake.instances


# ------------------------------------------------------------------------------------------------------------ adapters
def test_postconditions_skip_unmeasured_counts_honestly(fake):
    rt = runtime()
    results = adapters.candidate_postconditions(rt, {"boundary_edges": 4}, {"boundary_edges": 0})
    by_name = {r.operation: r for r in results}
    assert by_name["nonmanifold_edges_not_increased"].status == UNAVAILABLE
    assert "not measured" in by_name["nonmanifold_edges_not_increased"].reason
    assert not fake.instances or "verification" not in fake.instances[0].agents


def test_analysis_interpretation_sends_only_measured_findings(fake):
    seen = {}
    def capture(request):
        seen.update(request)
        return ok(request)
    fake.behaviours["reasoning"] = capture
    result = adapters.analysis_interpretation(runtime(), [{"code": "cadcon.mesh.open_boundaries", "severity": "warning", "message": "m", "secret": "x"}],
                                              {"triangles": 12, "bounds": {"min": [0]}, "unit": "mm"})
    assert result.ok
    sent = seen["context"]["geometry_evidence"]
    assert sent["summary"] == {"triangles": 12, "unit": "mm"} and "secret" not in str(sent)


# ------------------------------------------------------------------------------------------------------------ facade
def test_facade_keeps_legacy_shapes(fake):
    fake.behaviours["reasoning"] = ok
    rt = runtime()
    done = facade.mesh_repair_advice({"source": {}, "reconstructed": {}}, [], runtime=rt)
    assert done["status"] == "completed" and done["interpretation"] and done["record"]["evidence"][0]["kind"] == "ai_assisted"
    assert facade.mesh_repair_advice({}, [], enabled=False)["status"] == "disabled"
    assert facade.mesh_repair_advice({}, [], runtime=runtime(**{"agents.enabled": False}))["status"] == "disabled"


# ------------------------------------------------------------------------------------------------------ real agents
def _real(name):
    try:
        import src.agents.agent_factory as module
        from src.agents import verification  # noqa: F401
        agent = module.AgentFactory().get_agent(name)
        return agent
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"real {name} agent cannot be constructed in this environment: {type(exc).__name__}")


def test_real_verification_agent_checks_postconditions():
    _real("verification")
    rt = AgentRuntime(cfg.load_config(environ={}, force_reload=True))
    before = {"boundary_edges": 36, "nonmanifold_edges": 0, "inconsistent_winding_edges": 0}
    after = {"boundary_edges": 0, "nonmanifold_edges": 0, "inconsistent_winding_edges": 0}
    results = adapters.candidate_postconditions(rt, before, after)
    assert [r.assessment for r in results] == ["verified"] * 3
    assert all(r.evidence[0]["kind"] == helpers.VERIFIED for r in results)
    worse = adapters.candidate_postconditions(rt, before, {**after, "boundary_edges": 40})
    assert worse[0].assessment == "refuted" and worse[0].evidence[0]["kind"] == helpers.VERIFIED and "refuted" in worse[0].evidence[0]["claim"]
    rt.shutdown()


def test_real_spatial_agent_recovers_a_known_rigid_motion():
    _real("spatial")
    import math
    rt = AgentRuntime(cfg.load_config(environ={}, force_reload=True))
    source = [[x, y, z] for x in (0, 1, 2) for y in (0, 1) for z in (0, 1)]
    angle = 0.05
    target = [[math.cos(angle) * p[0] - math.sin(angle) * p[1] + .2, math.sin(angle) * p[0] + math.cos(angle) * p[1] - .1, p[2] + .05] for p in source]
    result = adapters.register_point_sets(rt, source, target)
    assert result.ok and result.result["rmse"] < 1e-6 and result.assessment == "converged"
    assert result.evidence[0]["kind"] == helpers.ESTIMATED, "registration is an estimate, not a measurement"
    bad = adapters.register_point_sets(rt, [[0, 0, 0]], [])
    assert not bad.ok and bad.status in (FAILED, INVALID)
    rt.shutdown()
