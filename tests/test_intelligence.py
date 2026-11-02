"""Repair policy, adaptive tolerance, ranking and engine verification/comparison improvements."""
from __future__ import annotations

import math

import pytest

import engine
import repair
from tests.test_repair import cube, cube_with_small_seam, facet_soup
from utils import config_loader as cfg


def settings(**over):
    return repair.RepairSettings.from_config(cfg.load_config(overrides=over, environ={}, force_reload=True))


def seam_cube(offset: float) -> engine.Mesh:
    mesh = facet_soup(cube())
    for face in mesh.faces[2:4]:
        for idx in face:
            mesh.vertices[idx][2] += offset
    return mesh


def scaled(mesh: engine.Mesh, factor: float, unit: str = "mm") -> engine.Mesh:
    return engine.Mesh([[x * factor for x in v] for v in mesh.vertices], [list(f) for f in mesh.faces], unit=unit)


# ----------------------------------------------------------------------------------------------------------- policies
def test_policy_ceilings_are_strictly_ordered_and_reported():
    ceilings = {mode: repair.recommend_tolerance(seam_cube(0.0001), settings(**{"repair.mode": mode}))["ceiling"]
                for mode in ("conservative", "balanced", "exploratory")}
    assert ceilings["conservative"] < ceilings["balanced"] < ceilings["exploratory"]
    assert repair.recommend_tolerance(seam_cube(0.0001), settings(**{"repair.mode": "balanced"}))["mode"] == "balanced"


def test_modes_decide_differently_on_a_gap_between_their_ceilings():
    mesh = seam_cube(0.012)     # 0.7 % of the diagonal: beyond conservative (0.1 %) and balanced (0.5 %), within exploratory (1 %)
    result = {mode: repair.recommend_tolerance(mesh, settings(**{"repair.mode": mode})) for mode in ("conservative", "balanced", "exploratory")}
    assert result["conservative"]["closure"] != "full" and result["balanced"]["closure"] != "full"
    assert result["exploratory"]["closure"] == "full"
    assert result["exploratory"]["tolerance"] > result["balanced"]["ceiling"]
    assert "exploratory" in result["exploratory"]["policy"].lower()


def test_max_tolerance_fraction_can_only_tighten_a_policy():
    tight = settings(**{"repair.mode": "exploratory", "repair.max_tolerance_fraction": 0.001})
    assert tight.policy.ceiling_fraction == pytest.approx(0.001)
    loose = settings(**{"repair.mode": "conservative", "repair.max_tolerance_fraction": 0.01})
    assert loose.policy.ceiling_fraction == repair.POLICIES["conservative"].ceiling_fraction


def test_automatic_tolerance_is_scale_and_unit_invariant():
    base = repair.recommend_tolerance(cube_with_small_seam())
    thousand = repair.recommend_tolerance(scaled(cube_with_small_seam(), 1000.0))
    assert thousand["tolerance"] / base["tolerance"] == pytest.approx(1000.0, rel=1e-6)
    assert thousand["closure"] == base["closure"]
    assert repair.recommend_tolerance(cube_with_small_seam())["unit"] == "mm"


def test_recommendation_is_measured_not_guessed():
    rec = repair.recommend_tolerance(cube_with_small_seam())
    assert rec["gap_profile"]["paired_vertices"] > 0 and rec["evidence"]
    assert rec["levels"] and rec["baseline_boundary_edges"] > rec["expected_boundary_edges"]
    exact = repair.recommend_tolerance(facet_soup(cube()))
    assert exact["tolerance"] == 0.0 and exact["closure"] == "full"


def test_already_closed_mesh_needs_no_reconstruction():
    assert repair.recommend_tolerance(cube())["needs_reconstruction"] is False


# ----------------------------------------------------------------------------------------------------- strategies/limits
def test_enabled_strategies_restrict_candidates_and_capability_report():
    source = facet_soup(cube()); source.faces = source.faces[:2] + source.faces[4:]
    everything = {c.strategy for c in repair.candidates(source, 0.0, 5, settings())}
    assert {"hole_fill", "planar_cap"} <= everything
    only_fill = settings(**{"repair.enabled_strategies": ["topology", "hole_fill"]})
    assert {c.strategy for c in repair.candidates(source, 0.0, 5, only_fill)} <= {"topology", "hole_fill"}
    info = repair.capability_info(only_fill)
    assert "planar_cap" in info["disabled"] and "hole_fill" in info["available"]


def test_candidate_limit_is_configured_and_tightens_for_large_meshes():
    s = settings(**{"repair.max_candidates": 3, "repair.large_mesh_triangles": 1000, "repair.large_mesh_max_candidates": 1})
    assert s.candidate_limit(10) == 3 and s.candidate_limit(10, 2) == 2 and s.candidate_limit(10, 99) == 3
    assert s.candidate_limit(5000) == 1


def test_patch_edge_limit_is_enforced():
    source = facet_soup(cube()); source.faces = source.faces[:2] + source.faces[4:]
    refused = repair.candidates(source, 0.0, 5, settings(**{"repair.max_patch_edges": 3}))
    assert not any(c.strategy in {"hole_fill", "planar_cap"} for c in refused)


def test_ranking_is_deterministic_and_prefers_closed_faithful_candidates():
    source = facet_soup(cube()); source.faces = source.faces[:2] + source.faces[4:]
    first = [(c.strategy, c.validation["status"]) for c in repair.candidates(source, 0.0, 5)]
    assert first == [(c.strategy, c.validation["status"]) for c in repair.candidates(source, 0.0, 5)]
    assert first[0][1].startswith("topologically_closed")
    statuses = [status for _, status in first]
    assert statuses == sorted(statuses, key=lambda s: not s.startswith("topologically_closed"))


def test_no_candidate_claims_solid_validity():
    source = facet_soup(cube()); source.faces = source.faces[:2] + source.faces[4:]
    for c in repair.candidates(source, 0.0, 5):
        assert c.validation["solid_validity"] != "valid"
        assert "unverified" in c.validation["status"] or c.validation["status"].startswith("still")


def test_automatic_tolerance_never_collapses_triangles_silently():
    mesh = seam_cube(0.0001)
    n = len(mesh.vertices)
    mesh.vertices += [[0.0, 0.0, 0.0], [1e-9, 0.0, 0.0], [0.0, 1e-9, 0.0]]
    mesh.faces.append([n, n + 1, n + 2])
    with pytest.raises(engine.GeometryError):
        repair.diagnose(mesh, 1e-6)                 # a manual tolerance that would collapse a triangle is refused
    rec = repair.recommend_tolerance(mesh)
    repair.diagnose(mesh, rec["tolerance"])         # the automatic one is always usable
    assert isinstance(rec["removed_faces"], int)


# ------------------------------------------------------------------------------------------------------ engine fidelity
def _summary(kind="triangle_mesh", **kw):
    base = {"representation": kind, "unit": "mm", "triangles": 12, "bounds": {"min": [0, 0, 0], "max": [1, 1, 1]}, "surface_area": 6.0}
    base.update(kw)
    return {"geometry_summary": base}


def test_fidelity_passes_identical_and_unit_scaled_output_and_labels_it_verified():
    out = engine.verify_fidelity(_summary(), _summary())
    assert out["passed"] and out["evidence"]["kind"] == "verified"
    meters = _summary(unit="m", bounds={"min": [0, 0, 0], "max": [.001, .001, .001]}, surface_area=6e-6)
    assert engine.verify_fidelity(_summary(), meters)["passed"]


@pytest.mark.parametrize("change, name", [
    ({"triangles": 11}, "triangle_count"),
    ({"bounds": {"min": [0, 0, 0], "max": [1, 1, 1.5]}}, "bounds_extents"),
    ({"surface_area": 5.0}, "surface_area"),
])
def test_fidelity_reports_real_violations(change, name):
    out = engine.verify_fidelity(_summary(), _summary(**change))
    assert not out["passed"] and name in out["violations"] and out["evidence"]["kind"] == "measured"


def test_fidelity_representation_change_is_a_violation_and_2d_entity_loss_informational():
    assert "representation" in engine.verify_fidelity(_summary(), _summary("drawing"))["violations"]
    drawing = lambda n: {"geometry_summary": {"representation": "drawing", "entities": n}}
    out = engine.verify_fidelity(drawing(10), drawing(8))
    assert out["passed"] and out["checks"][0]["status"] == "informational"


def test_surface_distance_is_labelled_approximate_and_scales_with_offset(tmp_path):
    a = cube(); b = scaled(cube(), 1.0)
    for v in b.vertices:
        v[2] += 0.1
    same = engine.surface_distance(a, cube())
    moved = engine.surface_distance(a, b)
    assert same["approximate"] is True and same["evidence"]["kind"] == "estimated"
    assert same["max"] < 1e-9 < moved["max"] <= 0.2


def test_sample_points_is_deterministic_and_bounded():
    mesh = cube()
    assert engine.sample_points(mesh, 10) == engine.sample_points(mesh, 10)
    assert len(engine.sample_points(mesh, 10)) <= 10


def test_configure_changes_limits_and_restores():
    before = engine.limits()
    try:
        assert engine.configure(max_triangles=123)["max_triangles"] == 123
        assert engine.configure()["max_triangles"] == 123           # None keeps
    finally:
        engine.configure(**before)
    assert engine.limits() == before
