"""Deterministic geometry and interchange tests (no downloaded model fixtures)."""
from __future__ import annotations

import json
import struct
import zlib
from pathlib import Path

import pytest

import engine


def _png_bytes(width: int = 3, height: int = 2) -> bytes:
    def chunk(kind: bytes, content: bytes) -> bytes:
        return (struct.pack(">I", len(content)) + kind + content
                + struct.pack(">I", zlib.crc32(kind + content) & 0xFFFFFFFF))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    pixels = (b"\x00" + bytes([216, 180, 0]) * width) * height
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b"")


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
    report = engine.analysis(source)
    content = engine.report_bytes(report, "sldc", preview_png=_png_bytes())
    package = sldc.loads_package(content)
    assert package.version == "1.1.0"
    assert package.document["title"] == "CADCon Geometry Analysis a"
    assert len(package.assets) == 1
    blocks = package.document["blocks"]
    assert any(block["type"] == "figure" and block["section"] == "cadcon-cover" for block in blocks)
    headings = [block["text"] for block in blocks if block["type"] == "heading"]
    assert headings == ["Geometry Summary", "Validation Coverage", "Findings", "Provenance"]
    assert all(block["attributes"]["page_break_before"] for block in blocks if block["type"] == "heading")
    assert package.document["styles"]["paragraph"]["report_heading"]["keep_with_next"] is True
    toc = [block for block in blocks if "slai.sldc.toc" in block.get("extensions", {})]
    assert len(toc) == len(headings) + 1
    assert toc[0]["extensions"]["slai.sldc.toc"]["role"] == "title"
    assert [b["text"] for b in toc[1:]] == headings
    assert report["source"]["sha256"] == package.document["metadata"]["source_sha256"]


@pytest.mark.parametrize("model,expected", [
    ("hinge.sgeo", "cadcon-analysis-hinge.sldc"),
    ("Gear-box-bottom.stl", "cadcon-analysis-Gear-box-bottom.sldc"),
    ("Assembly.final.glb", "cadcon-analysis-Assembly.final.sldc"),
    ("folder\\My bracket.stl", "cadcon-analysis-My bracket.sldc"),
    ("C:/parts/café.stl", "cadcon-analysis-cafe.sldc"),
    ("../...stl", "cadcon-analysis-model.sldc"),
])
def test_sldc_filename_uses_original_model_name(model: str, expected: str) -> None:
    data = {"report_kind": "cadcon.analysis", "source": {"name": model}}
    assert engine.report_filename(data, "sldc") == expected
    assert engine.report_filename(data, "json") == expected.removesuffix("sldc") + "json"


def test_sldc_conditional_ai_section(tmp_path: Path) -> None:
    sldc = pytest.importorskip("src.utils.sldc")
    source = tmp_path / "part.obj"
    source.write_text("v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n", encoding="utf-8")
    report = engine.analysis(source)
    report["ai_analysis"] = {"agent": "SLAI Spatial", "evidence": ["cadcon.mesh.open_boundaries"],
                             "summary": "This mesh has an open boundary.", "limitations": "No self-intersection test"}
    package = sldc.loads_package(engine.report_bytes(report, "sldc", preview_png=_png_bytes()))
    headings = [block["text"] for block in package.document["blocks"] if block["type"] == "heading"]
    assert headings[-1] == "AI Analysis"


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


def test_stl_to_sgeo_uses_shared_storage_instead_of_native_save(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    sgeo = pytest.importorskip("src.utils.sgeo")
    source = tmp_path / "triangle.stl"
    facet = struct.pack("<12fH", 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0)
    source.write_bytes(b"CADCon".ljust(80, b" ") + struct.pack("<I", 1) + facet)
    destination = tmp_path / "triangle.sgeo"

    def unavailable_native_writer(*_args: object, **_kwargs: object) -> None:
        raise OSError("filesystem does not support atomic no-clobber publication")

    monkeypatch.setattr(sgeo.SGEODocument, "save", unavailable_native_writer)
    result = engine.convert(source, destination, unit="mm")
    assert result["validated"] is True
    assert sgeo.SGEODocument.open(destination).count() == 1


def test_sgeo_export_survives_windows_rename_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    sgeo = pytest.importorskip("src.utils.sgeo")
    from applications.tools import storage

    source = tmp_path / "triangle.obj"
    source.write_text("v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n", encoding="utf-8")
    destination = tmp_path / "triangle.sgeo"

    def rejected_rename(*_args: object, **_kwargs: object) -> None:
        raise PermissionError("WinError 5: Access denied")

    with monkeypatch.context() as patch:
        patch.setattr(storage, "_RETRIES", 1)
        patch.setattr(storage.os, "replace", rejected_rename)
        result = engine.convert(source, destination)

    assert result["validated"] is True
    assert sgeo.SGEODocument.open(destination).count() == 1


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


def test_sdrw_export_uses_shared_storage_instead_of_native_save(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    sdrw = pytest.importorskip("src.utils.sdrw")
    source = tmp_path / "line.svg"
    source.write_text('<svg xmlns="http://www.w3.org/2000/svg"><line x1="0" y1="0" x2="10" y2="20"/></svg>', encoding="utf-8")
    destination = tmp_path / "line.sdrw"

    def unavailable_native_writer(*_args: object, **_kwargs: object) -> None:
        raise OSError("filesystem does not support atomic no-clobber publication")

    monkeypatch.setattr(sdrw.SDRWDocument, "save", unavailable_native_writer)
    result = engine.convert(source, destination)
    assert result["validated"] is True
    assert engine.inspect(destination)["geometry_summary"]["entities"] == 1


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
