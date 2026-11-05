"""Bounded repair algorithms, face provenance and native export regression tests."""
from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

import app as cadapp
import engine
import repair


def cube() -> engine.Mesh:
    points = [[0., 0., 0.], [1., 0., 0.], [1., 1., 0.], [0., 1., 0.],
              [0., 0., 1.], [1., 0., 1.], [1., 1., 1.], [0., 1., 1.]]
    faces = [[0, 2, 1], [0, 3, 2], [4, 5, 6], [4, 6, 7],
             [0, 1, 5], [0, 5, 4], [3, 7, 6], [3, 6, 2],
             [0, 4, 7], [0, 7, 3], [1, 2, 6], [1, 6, 5]]
    return engine.Mesh(points, faces, unit="mm")


def facet_soup(mesh: engine.Mesh) -> engine.Mesh:
    points, triangles = [], []
    for face in mesh.faces:
        start = len(points)
        points.extend([list(mesh.vertices[i]) for i in face])
        triangles.append([start, start + 1, start + 2])
    return engine.Mesh(points, triangles, unit=mesh.unit)


def cube_with_small_seam() -> engine.Mesh:
    mesh = facet_soup(cube())
    # The upper surface is offset by 0.0001 mm, leaving a near-coincident
    # seam that exact coordinate welding cannot close.
    for face in mesh.faces[2:4]:
        for idx in face:
            mesh.vertices[idx][2] += 0.0001
    return mesh


def test_exact_weld_recognizes_closed_facet_soup_without_new_faces():
    source = facet_soup(cube())
    diagnosis = repair.diagnose(source)
    assert diagnosis["source"]["boundary_edges"] == 36
    assert diagnosis["reconstructed"]["boundary_edges"] == 0
    assert diagnosis["reconstructed"]["nonmanifold_edges"] == 0
    result = repair.candidates(source)
    assert len(result) == 1 and result[0].strategy == "topology"
    assert result[0].added_faces == []
    assert result[0].validation["watertight"] is True
    assert len(result[0].mesh.faces) == len(source.faces)
    assert sorted(map(tuple, result[0].mesh.vertices)) == sorted(map(tuple, cube().vertices))


def test_open_cube_yields_two_distinct_real_red_patch_strategies():
    source = facet_soup(cube())
    source.faces = source.faces[:2] + source.faces[4:]  # Missing the two top triangles.
    result = repair.candidates(source)
    by_strategy = {candidate.strategy: candidate for candidate in result}
    assert set(by_strategy) == {"topology", "hole_fill", "planar_cap"}
    for strategy in ("hole_fill", "planar_cap"):
        candidate = by_strategy[strategy]
        assert candidate.added_faces
        assert all(idx >= len(source.faces) for idx in candidate.added_faces)
        assert candidate.validation["boundary_edges"] == 0
        assert candidate.validation["nonmanifold_edges"] == 0
        assert candidate.validation["consistent_winding"] is True
        assert candidate.validation["watertight"] is True
        assert candidate.mesh.faces[:len(source.faces)] == repair.weld(source).faces
        assert candidate.validation["solid_validity"].startswith("not_verified")


def test_shell_isolation_removes_false_shared_edges_without_creating_faces():
    left = cube()
    right = cube()
    # Two cubes meet geometrically along one edge. Welding creates incidence 4.
    right.vertices = [[x + 1, y + 1, z] for x, y, z in right.vertices]
    points = left.vertices + right.vertices
    faces = left.faces + [[i + len(left.vertices) for i in f] for f in right.faces]
    source = engine.Mesh(points, faces, unit="mm")
    exact = repair.weld(source)
    assert repair.validate(exact)["nonmanifold_edges"] > 0
    separated = repair._isolate_shells(exact)
    assert separated is not None
    assert repair.validate(separated)["nonmanifold_edges"] == 0
    assert len(separated.faces) == len(source.faces)
    assert repair.candidates(source, limit=1)[0].strategy == 'isolate_shells'


def test_invalid_welding_and_unavailable_methods_not_faked():
    with pytest.raises(engine.GeometryError, match="non-negative"):
        repair.weld(cube(), -0.01)
    with pytest.raises(engine.GeometryError, match="1%"):
        repair.weld(cube(), .5)
    assert "volumetric_reconstruction" in repair.capability_info()["unavailable"]
    assert repair.candidates(cube()) == []


def test_recommended_welding_tolerance_finds_real_near_coincident_seam():
    source = cube_with_small_seam()
    exact = repair.validate(repair.weld(source))
    assert exact["boundary_edges"] > 0
    suggestion = repair.recommend_tolerance(source)
    assert 0 < suggestion["tolerance"] <= suggestion["model_diagonal"] * .001
    assert suggestion["expected_boundary_edges"] == 0
    selected = repair.validate(repair.weld(source, suggestion["tolerance"]))
    assert selected["boundary_edges"] == 0
    assert selected["nonmanifold_edges"] == 0
    assert any(c.strategy == "tolerant_weld" for c in repair.candidates(source, suggestion["tolerance"]))


def test_auto_tolerance_stays_exact_when_welding_cannot_help():
    assert repair.recommend_tolerance(facet_soup(cube()))["tolerance"] == 0
    missing_top = facet_soup(cube())
    missing_top.faces = missing_top.faces[:2] + missing_top.faces[4:]
    suggestion = repair.recommend_tolerance(missing_top)
    assert suggestion["tolerance"] == 0
    assert suggestion["expected_boundary_edges"] > 0
    assert "genuine holes" in suggestion["reason"]


def _call(router, state, method, path, *, body=None, query=None):
    from applications.tools.server.router import Request
    action, params = router.match(method, path)
    raw = body if isinstance(body, bytes) else json.dumps(body).encode() if body is not None else b''
    request = Request(method, path, query or {}, {}, io.BytesIO(raw), len(raw) or None, params)
    return action(state, request)


def test_repair_api_preview_red_faces_and_native_sgeo_roundtrip(tmp_path: Path):
    app = cadapp.CADCon(tmp_path)
    router = cadapp.build_router()
    mesh = cube()
    mesh.faces = mesh.faces[:2] + mesh.faces[4:]
    source = tmp_path / "open.obj"
    source.write_bytes(engine._obj_bytes(mesh))
    app.files["source-id"] = source
    app.original_names["source-id"] = "open.obj"

    report = _call(router, app, 'POST', '/api/analyze', body={"id": "source-id"})

    info = _call(router, app, 'POST', '/api/repair/analyze', body={"id": "source-id"})
    assert info["diagnosis"]["reconstructed"]["boundary_edges"] == 4
    assert info["diagnosis"]["tolerance_mode"] == "automatic"
    assert info["diagnosis"]["recommendation"]["tolerance"] == 0
    result = _call(router, app, 'POST', '/api/repair/suggest', body={"id": "source-id", "use_agents": False})
    assert result["agents"]["status"] == "disabled"
    json_response = _call(router, app, 'GET', f'/api/reports/{report["id"]}', query={"format": "json"})
    saved_report = json.loads(json_response.body)
    assert saved_report["repair"]["diagnosis"]["reconstructed"]["boundary_edges"] == 4
    candidate = next(x for x in result["candidates"] if x["strategy"] == "hole_fill")
    preview = _call(router, app, 'GET', f'/api/repair/candidates/{candidate["id"]}/preview')
    assert preview["new_faces"] == [10, 11]
    assert len(preview["faces"]) == 12
    from tests.test_engine import _png_bytes
    _call(router, app, 'POST', f'/api/reports/{report["id"]}/preview',
          query={"source_id": "source-id"}, body=_png_bytes())
    _call(router, app, 'POST', f'/api/reports/{report["id"]}/repair-preview',
          query={"candidate_id": candidate["id"]}, body=_png_bytes(4, 3))
    from src.utils import sldc
    package = sldc.loads_package(_call(router, app, 'GET', f'/api/reports/{report["id"]}',
                                       query={"format": "sldc"}).body)
    assert len(package.assets) == 3  # CADCon logo, original, and repair screenshot.
    assert sum(block['type'] == 'figure' for block in package.document['blocks']) == 3
    exported = _call(router, app, 'POST', f'/api/repair/candidates/{candidate["id"]}/export', body={"format": "sgeo"})
    assert exported["export_verified"] is True
    assert exported["filename"] == "open-repaired-hole_fill.sgeo"
    blob = _call(router, app, 'GET', f'/api/outputs/{exported["id"]}')
    assert blob.download_name == exported["filename"]
    output = app.outputs[exported["id"]]
    assert output.read_bytes() == blob.body
    checked = engine.load_mesh(output)
    assert len(checked.faces) == 12
    assert repair.validate(checked)["watertight"] is True
    from src.utils.sgeo import SGEODocument
    sg = SGEODocument.open(output)
    record = sg.snapshot().objects[0]["metadata"]["cadcon:repair"]
    assert record["strategy"] == "hole_fill"
    assert record["added_face_indices"] == [10, 11]


def test_repair_api_auto_tolerance_and_manual_override(tmp_path: Path):
    app = cadapp.CADCon(tmp_path)
    router = cadapp.build_router()
    source = tmp_path / "seam.obj"
    source.write_bytes(engine._obj_bytes(cube_with_small_seam()))
    app.files["seam"] = source
    app.original_names["seam"] = source.name
    exact = _call(router, app, 'POST', '/api/repair/analyze',
                  body={"id": "seam", "tolerance": 0})["diagnosis"]
    assert exact["tolerance_mode"] == "manual"
    assert exact["reconstructed"]["boundary_edges"] > 0
    automatic = _call(router, app, 'POST', '/api/repair/analyze',
                      body={"id": "seam", "tolerance": "auto"})["diagnosis"]
    assert automatic["tolerance_mode"] == "automatic"
    assert automatic["tolerance"] > 0
    assert automatic["reconstructed"]["boundary_edges"] == 0
    variants = _call(router, app, 'POST', '/api/repair/suggest',
                     body={"id": "seam", "use_agents": False})
    assert variants["diagnosis"]["tolerance"] == automatic["tolerance"]
    assert any(item["strategy"] == "tolerant_weld" for item in variants["candidates"])


def test_large_candidate_preview_reserves_red_face_capacity(tmp_path):
    app = cadapp.CADCon(tmp_path)
    router = cadapp.build_router()
    large = engine.Mesh([[0., 0., 0.], [1., 0., 0.], [0., 1., 0.]],
                        [[0, 1, 2] for _ in range(50_001)], unit="mm")
    item = repair.Candidate("hole_fill", "Generated patch", large, [50_000],
                            "Test only", 50_000, {"status": "incomplete"})
    app.repair_candidates["large"] = ("source-id", item, "large.obj", "abc")
    data = _call(router, app, 'GET', '/api/repair/candidates/large/preview')
    assert data["partial"] is True
    assert len(data["faces"]) == 50_000
    assert data["new_faces"] == [49_999]


def test_repair_evidence_appears_in_native_sldc_without_extra_chapter(tmp_path):
    from src.utils import sldc
    from tests.test_engine import _png_bytes
    source = tmp_path / 'test.obj'
    source.write_bytes(engine._obj_bytes(cube()))
    report = engine.analysis(source)
    report['repair'] = {
        'diagnosis': repair.diagnose(facet_soup(cube())),
        'candidates': [{'label': 'Exact topology reconstruction', 'added_faces': 0,
                        'validation': {'status': 'topologically_closed_geometry_unverified'}}],
        'agents': {'status': 'disabled'},
    }
    package = sldc.loads_package(engine.report_bytes(report, 'sldc', preview_png=_png_bytes()))
    body = package.document['blocks']
    assert any(block.get('text') == 'Mesh repair and reconstructed topology' for block in body)
    titles = [block['text'] for block in body if block['type'] == 'heading']
    assert titles == ['Geometry Summary', 'Validation Coverage', 'Findings', 'Provenance']


def test_agent_bridge_records_fallback_without_inventing_geometry(monkeypatch):
    from slai.bridge import agents
    assert agents.mesh_repair_advice({}, [], enabled=False)["status"] == "disabled"
    # An unavailable native SLAI factory must not stop deterministic repair.
    import src.agents.agent_factory as factory_module
    monkeypatch.setattr(factory_module, 'AgentFactory', lambda: (_ for _ in ()).throw(RuntimeError('not configured')))
    outcome = agents.mesh_repair_advice({"source": {}, "reconstructed": {}}, [], timeout_seconds=1)
    assert outcome["status"] == "unavailable"
    assert "RuntimeError" in outcome["reason"]
