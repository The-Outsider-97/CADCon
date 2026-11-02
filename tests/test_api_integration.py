"""Routes wired to configuration and to the agent runtime (agents are faked at the factory so every outcome can be forced)."""
from __future__ import annotations

import json

import pytest

import app as cadapp
import engine
from slai.bridge import agents as facade
from tests.test_repair import cube, facet_soup
from tests.test_routes import _call
from utils import cadcon_errors as errors, config_loader as cfg

OPEN_BOX = (b"v 0 0 0\nv 1 0 0\nv 1 1 0\nv 0 1 0\nv 0 0 1\nv 1 0 1\nv 1 1 1\nv 0 1 1\n"
            b"f 1 3 2\nf 1 4 3\nf 5 6 7\nf 5 7 8\nf 1 2 6\nf 1 6 5\nf 2 3 7\nf 2 7 6\nf 3 4 8\nf 3 8 7\n")


class Agent:
    def __init__(self, reply):
        self.reply, self.requests = reply, []

    def perform_task(self, request):
        self.requests.append(request)
        return self.reply(request) if callable(self.reply) else self.reply


class Factory:
    replies: dict = {}
    created: list = []

    def __init__(self):
        self.agents = {}

    def get_agent(self, key):
        if key not in self.replies:
            raise RuntimeError(f"{key} is not available")
        agent = self.agents.setdefault(key, Agent(self.replies[key]))
        Factory.created.append((key, agent))
        return agent

    def release(self, key):
        pass

    def shutdown(self):
        pass


VERIFIED = {"execution_success": True, "verification_status": "verified",
            "formal_result": {"summary": "invariant holds", "provenance": {"method": "model_checking"}}}
REGISTERED = {"status": "ok", "result": {"transform": [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]], "rmse": 0.02, "iterations": 4, "converged": True}}
REASONED = {"status": "ok", "result": {"explanation": "Four boundary edges form one loop consistent with a missing face."}}


@pytest.fixture
def app_env(tmp_path, monkeypatch):
    import src.agents.agent_factory as module
    root = cadapp._activate_slai(None)
    Factory.replies, Factory.created = {"verification": VERIFIED, "spatial": REGISTERED, "reasoning": REASONED}, []
    monkeypatch.setattr(module, "AgentFactory", Factory)
    facade.shutdown_runtime()
    cfg.reset_config()

    def make(**overrides):
        config = cfg.load_config(overrides=overrides, environ={}, force_reload=True)
        return cadapp.CADCon(tmp_path, slai_root=root, config=config), cadapp.build_router()

    yield make
    facade.shutdown_runtime()
    cfg.reset_config()
    engine.configure(max_source_bytes=64 * 1024 * 1024, max_triangles=600_000, preview_triangle_limit=50_000)


def upload(router, state, data=OPEN_BOX, name="box.obj"):
    return _call(router, state, "POST", "/api/files/import", body=data, query={"name": name})


def test_repair_suggest_attaches_verified_postconditions_and_reasoning(app_env):
    state, router = app_env()
    source = upload(router, state)
    result = _call(router, state, "POST", "/api/repair/suggest", body={"id": source["id"]})
    assert result["agents"]["status"] == "completed" and "missing face" in result["agents"]["interpretation"]
    assert result["agents"]["evidence_records"][0]["kind"] == "ai_assisted"
    assert result["candidates"]
    for candidate in result["candidates"]:
        assert {c["assessment"] for c in candidate["agent_checks"]} == {"verified"}
        assert candidate["agent_checks"][0]["evidence"][0]["kind"] == "verified"
    reasoning = next(agent for key, agent in Factory.created if key == "reasoning")
    sent = json.dumps(reasoning.requests[0], default=str)
    assert "box.obj" not in sent and "source_sha256" not in sent, "only measured counts are sent, never names or paths"


def test_use_agents_false_loads_nothing_and_adds_no_agent_claims(app_env):
    state, router = app_env()
    result = _call(router, state, "POST", "/api/repair/suggest", body={"id": upload(router, state)["id"], "use_agents": False})
    assert result["agents"]["status"] == "disabled" and not Factory.created
    assert all("agent_checks" not in c for c in result["candidates"])


def test_agent_failure_leaves_deterministic_repair_intact(app_env):
    Factory.replies.clear()
    state, router = app_env()
    result = _call(router, state, "POST", "/api/repair/suggest", body={"id": upload(router, state)["id"]})
    assert result["agents"]["status"] == "unavailable" and result["candidates"]
    assert all(all(check["status"] == "unavailable" for check in c["agent_checks"]) for c in result["candidates"])
    assert all(c["validation"]["solid_validity"] != "valid" for c in result["candidates"])


def test_configured_candidate_limit_and_strategies_apply(app_env):
    state, router = app_env(**{"repair.max_candidates": 1})
    result = _call(router, state, "POST", "/api/repair/suggest", body={"id": upload(router, state)["id"], "use_agents": False, "max_candidates": 5})
    assert len(result["candidates"]) == 1
    state, router = app_env(**{"repair.enabled_strategies": ["topology", "planar_cap"]})
    result = _call(router, state, "POST", "/api/repair/suggest", body={"id": upload(router, state)["id"], "use_agents": False})
    assert {c["strategy"] for c in result["candidates"]} <= {"topology", "planar_cap"}


def test_repair_mode_reaches_the_recommendation(app_env):
    state, router = app_env(**{"repair.mode": "conservative"})
    out = _call(router, state, "POST", "/api/repair/analyze", body={"id": upload(router, state)["id"]})
    assert out["diagnosis"]["recommendation"]["mode"] == "conservative"
    assert out["capabilities"]["mode"] == "conservative"


def test_system_agents_and_config_endpoints_report_real_state(app_env):
    state, router = app_env()
    system = _call(router, state, "GET", "/api/system")
    assert [a["key"] for a in system["agents"]["integrations"]] == ["spatial", "reasoning", "verification"]
    assert system["limits"]["max_triangles"] == 600_000 and system["configuration"]["loaded_from"] == "file"
    assert system["agents"]["workflows"]["compare_registration"] == ["spatial"]
    assert system["slai_root"] is True, "production mode must not disclose the checkout path"
    assert all(a["state"] == "not_loaded" for a in system["agents"]["integrations"])
    probed = _call(router, state, "GET", "/api/agents", query={"probe": "1"})
    states = {a["key"]: a["state"] for a in probed["agents"]}
    assert states == {"spatial": "ready", "reasoning": "ready", "verification": "ready"}
    Factory.replies.pop("reasoning"); facade.shutdown_runtime()
    state2, router2 = app_env()
    degraded = {a["key"]: a["state"] for a in _call(router2, state2, "GET", "/api/agents", query={"probe": "1"})["agents"]}
    assert degraded["reasoning"] == "unavailable" and degraded["spatial"] == "ready"
    view = _call(router, state, "GET", "/api/config")
    assert view["values"]["repair"]["mode"] == "balanced" and "/" not in view["diagnostics"]["file"]


def test_disabled_agent_reports_disabled_not_unavailable(app_env):
    state, router = app_env(**{"agents.integrations": {"spatial": {"enabled": False}}})
    rows = {a["key"]: a for a in _call(router, state, "GET", "/api/agents", query={"probe": "1"})["agents"]}
    assert rows["spatial"]["state"] == "disabled" and rows["verification"]["state"] == "ready"
    assert not any(key == "spatial" for key, _ in Factory.created)


def test_config_reload_applies_new_limits_and_rejects_bad_files(app_env, tmp_path):
    path = tmp_path / "live.yaml"
    path.write_text("repair:\n  mode: conservative\n", encoding="utf-8")
    config = cfg.load_config(path, environ={}, force_reload=True)
    cfg.activate(config, path=path, environ={})
    state, router = cadapp.CADCon(tmp_path, config=config), cadapp.build_router()
    assert state.settings.mode == "conservative"
    path.write_text("repair:\n  mode: exploratory\ngeometry:\n  max_triangles: 5000\n", encoding="utf-8")
    view = _call(router, state, "POST", "/api/config/reload", body={})
    assert view["values"]["repair"]["mode"] == "exploratory" and state.settings.mode == "exploratory"
    assert engine.limits()["max_triangles"] == 5000
    path.write_text("repair:\n  mode: broken\n", encoding="utf-8")
    with pytest.raises(errors.ConfigurationError):
        _call(router, state, "POST", "/api/config/reload", body={})
    assert state.settings.mode == "exploratory", "a rejected reload leaves the running configuration untouched"


def test_upload_limits_come_from_configuration(app_env):
    state, router = app_env(**{"files.max_active_uploads": 1, "files.max_source_bytes": 1024 * 1024})
    upload(router, state)
    from applications.tools.errors import LimitExceeded
    with pytest.raises(LimitExceeded):
        upload(router, state, name="second.obj")
    state, router = app_env(**{"files.max_source_bytes": 1024 * 1024})
    with pytest.raises(LimitExceeded):
        upload(router, state, data=b"x" * (1024 * 1024 + 1), name="huge.obj")


def test_default_unit_policy_and_report_prefix_are_configurable(app_env):
    state, router = app_env(**{"geometry.default_unit": "m", "reporting.filename_prefix": "acme-report"})
    source = upload(router, state)
    inspected = _call(router, state, "POST", "/api/inspect", body={"id": source["id"]})
    assert inspected["geometry_summary"]["unit"] == "m"
    analysed = _call(router, state, "POST", "/api/analyze", body={"id": source["id"]})
    assert analysed["filenames"]["json"].startswith("acme-report")


def test_analysis_cache_hits_and_can_be_disabled(app_env):
    state, router = app_env()
    source = upload(router, state)
    _call(router, state, "POST", "/api/analyze", body={"id": source["id"]})
    first = _call(router, state, "POST", "/api/analyze", body={"id": source["id"]})
    assert state.cache_stats["hits"] == 1
    first["report"]["source"]["name"] = "mutated"
    again = _call(router, state, "POST", "/api/analyze", body={"id": source["id"]})
    assert again["report"]["source"]["name"] == "box.obj", "cached results are copies"
    off, router_off = app_env(**{"performance.analysis_cache_entries": 0})
    src = upload(router_off, off)
    _call(router_off, off, "POST", "/api/analyze", body={"id": src["id"]}); _call(router_off, off, "POST", "/api/analyze", body={"id": src["id"]})
    assert off.cache_stats["hits"] == 0


def test_compare_registration_is_opt_in_and_labelled_an_estimate(app_env):
    state, router = app_env()
    left = upload(router, state)
    right = upload(router, state, data=OPEN_BOX.replace(b"v 1 1 1", b"v 1 1 1.2"), name="box2.obj")
    plain = _call(router, state, "POST", "/api/compare", body={"left": left["id"], "right": right["id"]})
    assert "registration" not in plain and not any(key == "spatial" for key, _ in Factory.created)
    assert plain["surface_distance"]["approximate"] is True
    registered = _call(router, state, "POST", "/api/compare", body={"left": left["id"], "right": right["id"], "register": True})
    reg = registered["registration"]
    assert reg["status"] == "completed" and reg["evidence"][0]["kind"] == "estimated" and reg["direction"] == "right model onto left model"
    assert reg["result"]["rmse"] == 0.02 and reg["samples"] > 3
    identical = _call(router, state, "POST", "/api/compare", body={"left": left["id"], "right": left["id"], "register": True})
    assert identical["registration"]["status"] == "not_applicable"


def test_compare_registration_degrades_when_spatial_agent_is_missing(app_env):
    Factory.replies.pop("spatial")
    state, router = app_env()
    left, right = upload(router, state), upload(router, state, data=OPEN_BOX.replace(b"v 1 1 1", b"v 1 1 1.2"), name="b2.obj")
    out = _call(router, state, "POST", "/api/compare", body={"left": left["id"], "right": right["id"], "register": True})
    assert out["status"] == "modified" and out["registration"]["status"] == "unavailable" and out["surface_distance"]


def test_interpretation_is_opt_in_attached_to_the_report_and_honoured_by_the_report_switch(app_env):
    state, router = app_env()
    source = upload(router, state)
    analysed = _call(router, state, "POST", "/api/analyze", body={"id": source["id"]})
    assert not Factory.created, "analysis alone never calls an agent"
    outcome = _call(router, state, "POST", f"/api/reports/{analysed['id']}/interpret", body={})
    assert outcome["status"] == "completed" and outcome["result"]["interpretation"]
    payload = json.loads(_call(router, state, "GET", f"/api/reports/{analysed['id']}", query={"format": "json"}).body)
    assert payload["ai_analysis"]["summary"] and "advisory" in payload["ai_analysis"]["limitations"]
    quiet, router_quiet = app_env(**{"reporting.include_ai_analysis": False})
    ident = _call(router_quiet, quiet, "POST", "/api/analyze", body={"id": upload(router_quiet, quiet)["id"]})
    _call(router_quiet, quiet, "POST", f"/api/reports/{ident['id']}/interpret", body={})
    assert "ai_analysis" not in json.loads(_call(router_quiet, quiet, "GET", f"/api/reports/{ident['id']}", query={"format": "json"}).body)


def test_unexpected_defects_are_not_disguised_as_user_errors(app_env, monkeypatch):
    state, router = app_env()
    source = upload(router, state)
    monkeypatch.setattr(engine, "inspect", lambda *a, **k: (_ for _ in ()).throw(KeyError("internal bug")))
    with pytest.raises(KeyError):
        _call(router, state, "POST", "/api/inspect", body={"id": source["id"]})


def test_expected_conditions_become_typed_cadcon_errors(app_env):
    state, router = app_env()
    source = upload(router, state)
    with pytest.raises(errors.CADConError) as caught:
        _call(router, state, "POST", "/api/convert", body={"id": source["id"], "format": "dxf"})
    assert caught.value.status == 400 and caught.value.code
