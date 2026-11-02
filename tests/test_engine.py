"""Deterministic geometry and interchange tests (no downloaded model fixtures)."""
from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest

import engine


def test_single_triangle_counts(tmp_path: Path) -> None:
    source = tmp_path / "triangle.obj"
    source.write_text("v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n", encoding="utf-8")
    result = engine.inspect(source)
    summary = result["geometry_summary"]
    assert (summary["vertices"], summary["edges"], summary["polygons"]) == (3, 3, 1)
    assert summary["boundary_edges"] == 3
    assert summary["watertight"] is False
    assert summary["volume"] is None
    assert summary["surface_area"] == pytest.approx(0.5)


def test_square_with_shared_edge(tmp_path: Path) -> None:
    source = tmp_path / "square.obj"
    source.write_text("v 0 0 0\nv 1 0 0\nv 1 1 0\nv 0 1 0\nf 1 2 3\nf 1 3 4\n", encoding="utf-8")
    result = engine.inspect(source)["geometry_summary"]
    assert result["vertices"] == 4
    assert result["edges"] == 5
    assert result["polygons"] == 2
    assert result["boundary_edges"] == 4


def test_binary_stl_preserves_source_record_semantics(tmp_path: Path) -> None:
    source = tmp_path / "square.stl"
    triangles = [[(0, 0, 0), (1, 0, 0), (1, 1, 0)], [(0, 0, 0), (1, 1, 0), (0, 1, 0)]]
    buffer = bytearray(b"CADCon".ljust(80, b" ") + struct.pack("<I", 2))
    for face in triangles:
        buffer.extend(struct.pack("<12fH", *(0.0, 0.0, 1.0, *face[0], *face[1], *face[2], 0)))
    source.write_bytes(buffer)
    result = engine.inspect(source)["geometry_summary"]
    assert (result["vertices"], result["edges"], result["polygons"]) == (6, 6, 2)
    assert result["distinct_coordinate_vertices"] == 4


def test_ascii_stl_requires_well_formed_facets(tmp_path: Path) -> None:
    source = tmp_path / "triangle.stl"
    source.write_text("""solid triangle
facet normal 0 0 1
  outer loop
    vertex 0 0 0
    vertex 1 0 0
    vertex 0 1 0
  endloop
endfacet
endsolid triangle
""", encoding="ascii")
    assert engine.inspect(source)["geometry_summary"]["triangles"] == 1
    source.write_text("solid wrong\nvertex 0 0 0\nendfacet\nendsolid wrong", encoding="ascii")
    with pytest.raises(engine.GeometryError, match="Malformed ASCII"):
        engine.inspect(source)


def test_invalid_obj_indices_are_rejected(tmp_path: Path) -> None:
    source = tmp_path / "bad.obj"
    source.write_text("v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 7\n", encoding="utf-8")
    with pytest.raises(engine.GeometryError):
        engine.inspect(source)


def test_comparison_does_not_claim_equivalence_on_matching_counts(tmp_path: Path) -> None:
    one, two = tmp_path / "one.obj", tmp_path / "two.obj"
    one.write_text("v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n", encoding="utf-8")
    two.write_text("v 0 0 0\nv 0 1 0\nv 1 0 0\nf 1 3 2\n", encoding="utf-8")
    assert engine.compare(one, one)["status"] == "identical"
    assert engine.compare(one, two)["status"] == "geometrically_equivalent"


def test_json_report(tmp_path: Path) -> None:
    source = tmp_path / "a.obj"
    source.write_text("v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n", encoding="utf-8")
    report = engine.analysis(source)
    result = json.loads(engine.report_bytes(report, "json"))
    assert result["source"]["sha256"] == report["provenance"]["source_sha256"]
    assert result["findings"][0]["code"] == "cadcon.mesh.open_boundaries"
    assert result["findings"][0]["measured_value"] == 3
    assert "self-intersection" in result["validation"]["not_executed"]


def test_sldc_report_roundtrip(tmp_path: Path) -> None:
    sldc = pytest.importorskip("src.utils.sldc")
    source = tmp_path / "a.obj"
    source.write_text("v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n", encoding="utf-8")
    content = engine.report_bytes(engine.analysis(source), "sldc")
    assert sldc.loads_package(content).document["title"] == "CADCon Geometry Analysis"


def test_native_sgeo_and_glb_conversion(tmp_path: Path) -> None:
    sgeo = pytest.importorskip("src.utils.sgeo")
    source = tmp_path / "square.obj"
    source.write_text("v 0 0 0\nv 1 0 0\nv 1 1 0\nv 0 1 0\nf 1 2 3\nf 1 3 4\n", encoding="utf-8")
    native = tmp_path / "square.sgeo"
    converted = engine.convert(source, native)
    assert converted["validated"]
    assert sgeo.SGEODocument.open(native).count() == 1
    glb = tmp_path / "square.glb"
    assert engine.convert(native, glb)["validated"]
    assert glb.stat().st_size > 0


def test_native_unit_scaling_to_unitless_stl_and_obj(tmp_path: Path) -> None:
    sgeo = pytest.importorskip("src.utils.sgeo")
    source = tmp_path / "metre_box.sgeo"
    scene = sgeo.SGEODocument.new("Metre box", unit="m")
    scene.add_box(width=1, depth=1, height=1)
    scene.save(source)

    stl = tmp_path / "millimetre_box.stl"
    conversion = engine.convert(source, stl, unit="mm")
    assert conversion["validated"]
    assert conversion["source_sha256"] == engine.inspect(source)["source"]["sha256"]
    assert conversion["target_sha256"] == engine.inspect(stl)["source"]["sha256"]
    assert conversion["duration_ms"] >= 0
    assert any("millimetre" in warning or "mm" in warning for warning in conversion["warnings"])
    stl_bounds = engine.inspect(stl)["geometry_summary"]["bounds"]
    assert max(upper - lower for lower, upper in zip(stl_bounds["min"], stl_bounds["max"])) == pytest.approx(1000)

    obj = tmp_path / "centimetre_box.obj"
    engine.convert(source, obj, unit="cm")
    obj_bounds = engine.inspect(obj, unit="cm")["geometry_summary"]["bounds"]
    assert max(upper - lower for lower, upper in zip(obj_bounds["min"], obj_bounds["max"])) == pytest.approx(100)
    # Different authored units, identical world-space coordinates in metres.
    assert engine.compare(source, obj, unit="cm")["status"] == "geometrically_equivalent"


def test_capabilities_routes_are_directional() -> None:
    registry = engine.capabilities()
    assert "routes" in registry
    assert "step" in registry["conditional"]
    assert "step" not in registry["routes"].get("obj", [])
    assert "ifc" not in registry["routes"].get("obj", [])
    assert "obj" not in registry["routes"].get("obj", [])
    assert "obj" in registry["routes"]["stl"]


def test_unsupported_conversion_fails_closed(tmp_path: Path) -> None:
    source = tmp_path / "a.obj"
    source.write_text("v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n", encoding="utf-8")
    with pytest.raises(engine.GeometryError):
        engine.convert(source, tmp_path / "out.step")
    assert not (tmp_path / "out.step").exists()


def test_strict_exchange_refuses_unpreserved_obj_records(tmp_path: Path) -> None:
    source = tmp_path / "material.obj"
    source.write_text("v 0 0 0\nv 1 0 0\nv 0 1 0\nmtllib example.mtl\nf 1 2 3\n", encoding="utf-8")
    destination = tmp_path / "result.sgeo"
    with pytest.raises(engine.GeometryError, match="not preserved"):
        engine.convert(source, destination, policy="strict")
    assert not destination.exists()


def test_svg_sdrw_dxf_roundtrip(tmp_path: Path) -> None:
    pytest.importorskip("ezdxf")
    source = tmp_path / "line.svg"
    source.write_text('<svg xmlns="http://www.w3.org/2000/svg"><line x1="0" y1="0" x2="10" y2="20"/></svg>', encoding="utf-8")
    drawing = tmp_path / "line.sdrw"
    assert engine.convert(source, drawing)["validated"]
    result = engine.inspect(drawing)
    assert result["geometry_summary"]["entities"] == 1
    dxf = tmp_path / "line.dxf"
    assert engine.convert(drawing, dxf)["validated"]


def test_comparison_report_sldc(tmp_path: Path) -> None:
    sldc = pytest.importorskip("src.utils.sldc")
    one, two = tmp_path / "one.obj", tmp_path / "two.obj"
    one.write_text("v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n", encoding="utf-8")
    two.write_text("v 0 0 0\nv 2 0 0\nv 0 1 0\nf 1 2 3\n", encoding="utf-8")
    result = {"report_kind": "cadcon.comparison", **engine.compare(one, two)}
    blob = engine.report_bytes(result, "sldc")
    assert sldc.loads_package(blob).document["title"] == "CADCon Model Comparison"


def test_opt_in_slai_spatial_nearest(tmp_path: Path) -> None:
    sgeo = pytest.importorskip("src.utils.sgeo")
    pytest.importorskip("src.agents.spatial")
    source = tmp_path / "two.sgeo"
    scene = sgeo.SGEODocument.new("Two", unit="m")
    scene.add_box(width=1, depth=1, height=1, name="Origin", position=(0, 0, 0))
    scene.add_box(width=1, depth=1, height=1, name="Far", position=(10, 0, 0))
    scene.save(source)
    result = engine.spatial_nearest(source, [0.0, 0.0, 0.0], unit="m")
    assert result["backend"] == "src.agents.spatial.SpatialQueries"
    assert result["entity_count"] == 2
    assert len(result["query"]["entity_ids"]) == 1
    assert result["query"]["distances"][0] == pytest.approx(0)
