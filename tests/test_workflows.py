"""Workflows A-F from the acceptance plan, driven through the real router with real geometry code. D uses the real SLAI Verification and
Spatial agents (skipped where they cannot be constructed); E forces every agent to be unavailable."""
from __future__ import annotations

import json
import struct

import pytest

import app as cadapp
import engine
import repair
from slai.bridge import agents as facade
from src.utils import sldc
from tests.test_api_integration import OPEN_BOX
from tests.test_engine import _png_bytes
from tests.test_routes import _call
from utils import config_loader as cfg


@pytest.fixture
def env(tmp_path):
    root = cadapp._activate_slai(None)
    facade.shutdown_runtime(); cfg.reset_config()

    def make(**overrides):
        config = cfg.load_config(overrides=overrides, environ={}, force_reload=True)
        return cadapp.CADCon(tmp_path, slai_root=root, config=config), cadapp.build_router()

    yield make
    facade.shutdown_runtime(); cfg.reset_config()
    engine.configure(max_source_bytes=64 * 1024 * 1024, max_triangles=600_000, preview_triangle_limit=50_000)


def put(router, state, data, name):
    return _call(router, state, "POST", "/api/files/import", body=data, query={"name": name})


def test_workflow_a_import_inspect_analyse_report(env):
    state, router = env(**{"agents.enabled": False})
    source = put(router, state, OPEN_BOX, "bracket.obj")
    inspected = _call(router, state, "POST", "/api/inspect", body={"id": source["id"]})
    summary = inspected["geometry_summary"]
    assert summary["triangles"] == 10 and summary["boundary_edges"] == 4 and summary["watertight"] is False
    analysed = _call(router, state, "POST", "/api/analyze", body={"id": source["id"]})
    assert [f["code"] for f in analysed["report"]["findings"]] == ["cadcon.mesh.open_boundaries"]
    assert "self-intersection" in analysed["report"]["validation"]["not_executed"]
    payload = json.loads(_call(router, state, "GET", f"/api/reports/{analysed['id']}", query={"format": "json"}).body)
    assert payload["source"]["name"] == "bracket.obj" and "ai_analysis" not in payload
    _call(router, state, "POST", f"/api/reports/{analysed['id']}/preview", body=_png_bytes(), query={"source_id": source["id"]})
    package = sldc.loads_package(_call(router, state, "GET", f"/api/reports/{analysed['id']}", query={"format": "sldc"}).body)
    assert any(block.get("text") == "Geometry Summary" for block in package.document["blocks"])
    assert analysed["filenames"]["sldc"] == "cadcon-analysis-bracket.sldc"


def test_workflow_b_repair_rank_preview_validate_export(env):
    state, router = env(**{"agents.enabled": False})
    source = put(router, state, OPEN_BOX, "box.obj")
    plan = _call(router, state, "POST", "/api/repair/analyze", body={"id": source["id"]})
    recommendation = plan["diagnosis"]["recommendation"]
    assert recommendation["tolerance"] >= 0 and recommendation["mode"] == "balanced" and plan["diagnosis"]["source"]["boundary_edges"] == 4
    suggested = _call(router, state, "POST", "/api/repair/suggest", body={"id": source["id"]})
    candidates = suggested["candidates"]
    assert candidates and candidates[0]["validation"]["status"].startswith("topologically_closed")
    best = candidates[0]
    preview = _call(router, state, "GET", f"/api/repair/candidates/{best['id']}/preview")
    assert preview["new_faces"], "the repair adds real, identifiable faces"
    assert len(preview["faces"]) > 10 or preview["partial"] is False
    exported = _call(router, state, "POST", f"/api/repair/candidates/{best['id']}/export", body={"format": "obj"})
    assert exported["export_verified"] is True and exported["filename"].endswith("-repaired-" + best["strategy"] + ".obj")
    path = state.outputs[exported["id"]]
    reopened = engine.load_mesh(path, unit="mm")
    check = repair.validate(reopened)
    assert check["boundary_edges"] == 0 and check["topologically_closed"] is True
    assert check["solid_validity"] != "valid", "closure is never reported as solid validity"
    assert len(reopened.faces) == 10 + best["added_faces"]
    original = engine.load_mesh(state.get_file(source["id"]), unit="mm")
    assert len(original.faces) == 10, "the original model is never modified"


def test_workflow_c_conversion_verification(env):
    state, router = env(**{"agents.enabled": False})
    source = put(router, state, OPEN_BOX, "part.obj")
    capabilities = _call(router, state, "GET", "/api/capabilities")
    assert "sgeo" in capabilities["routes"]["obj"]
    converted = _call(router, state, "POST", "/api/convert", body={"id": source["id"], "format": "sgeo", "policy": "best_effort"})
    assert converted["validated"] is True and converted["fidelity"]["passed"] is True
    assert converted["fidelity"]["evidence"]["kind"] == "verified" and converted["target"] == "part.sgeo"
    reread = engine.inspect(state.outputs[converted["id"]], unit="mm")
    assert reread["geometry_summary"]["triangles"] == 10
    download = _call(router, state, "GET", f"/api/outputs/{converted['id']}")
    assert download.body == state.outputs[converted["id"]].read_bytes()
    with pytest.raises(Exception) as refused:
        _call(router, state, "POST", "/api/convert", body={"id": source["id"], "format": "dxf"})
    assert "convert" in str(refused.value).lower() or "route" in str(refused.value).lower() or refused.value.status == 400


def _real_agents():
    try:
        import src.agents.agent_factory as module
        factory = module.AgentFactory()
        factory.get_agent("verification"); factory.get_agent("spatial")
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"real Verification/Spatial agents cannot be constructed here: {type(exc).__name__}")


def test_workflow_d_agent_assisted_results_are_distinguishable(env):
    _real_agents()
    state, router = env(**{"agents.integrations": {"reasoning": {"enabled": False}}})
    left = put(router, state, OPEN_BOX, "a.obj")
    right = put(router, state, OPEN_BOX.replace(b"v 1 1 1", b"v 1 1 1.2"), "b.obj")
    suggested = _call(router, state, "POST", "/api/repair/suggest", body={"id": left["id"]})
    best = suggested["candidates"][0]
    assert best["validation"]["status"].startswith("topologically_closed")             # deterministic result
    assert {c["assessment"] for c in best["agent_checks"]} == {"verified"}               # agent-verified claims, listed separately
    assert {c["evidence"][0]["kind"] for c in best["agent_checks"]} == {"verified"}
    assert suggested["agents"]["status"] == "disabled"                                  # reasoning off: nothing claimed
    compared = _call(router, state, "POST", "/api/compare", body={"left": left["id"], "right": right["id"], "register": True})
    registration = compared["registration"]
    assert registration["status"] == "completed" and registration["evidence"][0]["kind"] == "estimated"
    assert compared["surface_distance"]["evidence"]["kind"] == "estimated"
    assert compared["status"] == "modified" and "bounds" in compared["differences"]      # deterministic findings remain separate
    assert {entry["agent"] for entry in state.runtime.metrics()["recent_calls"]} == {"verification", "spatial"}


def test_workflow_e_unavailable_agents_never_block_or_fabricate(env, monkeypatch):
    import src.agents.agent_factory as module

    class Broken:
        def __init__(self):
            raise RuntimeError("no agent runtime on this machine")
    monkeypatch.setattr(module, "AgentFactory", Broken)
    state, router = env()
    source = put(router, state, OPEN_BOX, "box.obj")
    analysed = _call(router, state, "POST", "/api/analyze", body={"id": source["id"]})
    assert analysed["report"]["findings"], "deterministic analysis continues"
    interpret = _call(router, state, "POST", f"/api/reports/{analysed['id']}/interpret", body={})
    assert interpret["status"] == "unavailable" and "RuntimeError" in interpret["reason"] and interpret["result"] is None
    suggested = _call(router, state, "POST", "/api/repair/suggest", body={"id": source["id"]})
    assert suggested["candidates"] and suggested["agents"]["status"] == "unavailable"
    assert all(check["result"] is None and check["assessment"] is None for c in suggested["candidates"] for check in c["agent_checks"])
    payload = json.loads(_call(router, state, "GET", f"/api/reports/{analysed['id']}", query={"format": "json"}).body)
    assert "ai_analysis" not in payload
    status = {a["key"]: a["state"] for a in _call(router, state, "GET", "/api/agents")["agents"]}
    assert set(status.values()) <= {"unavailable", "not_loaded"}


def test_workflow_f_configuration_changes_behaviour(env, tmp_path):
    path = tmp_path / "workflow.yaml"
    seam = (b"v 0 0 0\nv 1 0 0\nv 1 1 0\nv 0 1 0\nv 0 0 1\nv 1 0 1\nv 1 1 1\nv 0 1 1\n"
            b"v 0 0 1.012\nv 1 0 1.012\nv 1 1 1.012\nv 0 1 1.012\n"
            b"f 1 3 2\nf 1 4 3\nf 9 10 11\nf 9 11 12\nf 1 2 6\nf 1 6 5\nf 2 3 7\nf 2 7 6\nf 3 4 8\nf 3 8 7\nf 1 5 8\nf 1 8 4\n")
    outcomes = {}
    for mode in ("balanced", "exploratory"):
        path.write_text(f"repair:\n  mode: {mode}\nagents:\n  enabled: false\n", encoding="utf-8")
        config = cfg.load_config(path, environ={}, force_reload=True)
        cfg.activate(config, path=path, environ={})
        state, router = cadapp.CADCon(tmp_path / mode, config=config), cadapp.build_router()
        source = put(router, state, seam, "seam.obj")
        diagnosis = _call(router, state, "POST", "/api/repair/analyze", body={"id": source["id"]})["diagnosis"]
        outcomes[mode] = (diagnosis["recommendation"]["mode"], diagnosis["recommendation"]["closure"], diagnosis["tolerance"])
    assert outcomes["balanced"][0] == "balanced" and outcomes["exploratory"][0] == "exploratory"
    assert outcomes["balanced"][1] != "full" and outcomes["exploratory"][1] == "full" and outcomes["exploratory"][2] > outcomes["balanced"][2]
    # a live edit takes effect on reload, and a limit rejects the next import
    path.write_text("files:\n  max_active_uploads: 1\ngeometry:\n  max_triangles: 5000\nagents:\n  enabled: false\n", encoding="utf-8")
    _call(router, state, "POST", "/api/config/reload", body={})
    assert state.settings.mode == "balanced" and engine.limits()["max_triangles"] == 5000
    from applications.tools.errors import LimitExceeded
    with pytest.raises(LimitExceeded):
        put(router, state, OPEN_BOX, "second.obj")
