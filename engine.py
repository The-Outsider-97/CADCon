"""CADCon's deterministic CAD inspection, conversion and comparison service.

The SLAI spatial documents remain the owners of SGEO, SDRW and their exchange
adapters. Only the missing OBJ interchange and source-level STL counting live
here. No agent is allowed to substitute measurements for this module's output.
"""
from __future__ import annotations

import hashlib
import json
import math
import struct
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MAX_SOURCE_BYTES = 64 * 1024 * 1024
MAX_TRIANGLES = 600_000
PREVIEW_TRIANGLE_LIMIT = 50_000
POLICIES = ("strict", "lossless_where_supported", "allow_approximation", "best_effort")
THREE_D = {".stl", ".obj", ".sgeo", ".glb", ".ifc"}
TWO_D = {".sdrw", ".dxf", ".svg", ".dwg"}
NATIVE_3D_OUTPUT = {".sgeo", ".stl", ".glb", ".obj"}
NATIVE_2D_OUTPUT = {".sdrw", ".dxf", ".svg", ".dwg"}
OPTIONAL_FORMATS = {".step", ".stp", ".sat", ".fbx", ".gltf", ".ply", ".3mf", ".igs", ".iges"}


class GeometryError(ValueError):
    """A malformed geometry or an unsupported/unsafe exchange operation."""


@dataclass
class Mesh:
    vertices: list[list[float]]
    faces: list[list[int]]
    unit: str | None = None
    source_polygons: int | None = None
    warnings: list[str] = field(default_factory=list)


def _regular_source(path: Path) -> Path:
    if path.is_symlink() or not path.is_file():
        raise GeometryError("The source must be an existing, regular file")
    if not 0 < path.stat().st_size <= MAX_SOURCE_BYTES:
        raise GeometryError("Source file is empty or exceeds the 64 MiB input limit")
    return path


def _length_unit(unit: str) -> str:
    """Validate user-declared units through SLAI's authoritative unit registry."""
    from src.utils.spatial_formats.common.units import require_unit
    return require_unit(unit)


def _number(value: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise GeometryError("Invalid geometry coordinate") from exc
    if not math.isfinite(number):
        raise GeometryError("Geometry contains a non-finite coordinate")
    return number


def _validated_mesh(mesh: Mesh) -> Mesh:
    if len(mesh.faces) > MAX_TRIANGLES or len(mesh.vertices) > MAX_TRIANGLES * 3:
        raise GeometryError("Model exceeds the geometry complexity limit")
    for vertex in mesh.vertices:
        if len(vertex) != 3 or not all(math.isfinite(n) for n in vertex):
            raise GeometryError("Vertices must contain three finite coordinates")
    for face in mesh.faces:
        if len(face) != 3 or len(set(face)) != 3 or any(i < 0 or i >= len(mesh.vertices) for i in face):
            raise GeometryError("Triangle contains invalid vertex indices")
    return mesh


def read_obj(path: Path) -> Mesh:
    """Bounded Wavefront geometry subset; unsupported rendering records are noted."""
    vertices: list[list[float]] = []
    faces: list[list[int]] = []
    polygon_count = 0
    ignored: set[str] = set()
    try:
        with _regular_source(path).open("r", encoding="utf-8-sig", errors="strict") as stream:
            for raw in stream:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split()
                if parts[0] == "v":
                    if len(parts) < 4:
                        raise GeometryError("OBJ vertex has fewer than three coordinates")
                    vertices.append([_number(v) for v in parts[1:4]])
                elif parts[0] == "f":
                    if len(parts) < 4:
                        raise GeometryError("OBJ face has fewer than three vertices")
                    if len(parts) > 4:
                        raise GeometryError("OBJ polygons with more than three vertices require a validated triangulation backend")
                    indices = []
                    for token in parts[1:]:
                        try:
                            raw_index = int(token.split("/", 1)[0])
                        except ValueError as exc:
                            raise GeometryError("OBJ face contains an invalid vertex index") from exc
                        index = raw_index - 1 if raw_index > 0 else len(vertices) + raw_index
                        if not 0 <= index < len(vertices):
                            raise GeometryError("OBJ face refers to a nonexistent vertex")
                        indices.append(index)
                    if len(set(indices)) != len(indices):
                        raise GeometryError("OBJ face repeats a vertex")
                    polygon_count += 1
                    faces.append(indices[:3])
                    if len(indices) == 4:
                        # A general non-planar/concave quad cannot be triangulated safely by a fan.
                        a, b, c, d = (vertices[i] for i in indices)
                        cross = _cross(_subtract(b, a), _subtract(c, a))
                        if abs(_dot(cross, _subtract(d, a))) > 1e-8 * max(1.0, _length(cross)):
                            raise GeometryError("Non-planar OBJ quad requires a geometry triangulation backend")
                        turns = [_dot(cross, _cross(_subtract(v, u), _subtract(w, v)))
                                 for u, v, w in ((a, b, c), (b, c, d), (c, d, a), (d, a, b))]
                        if _length(cross) <= 1e-15 or any(turn <= 1e-15 for turn in turns):
                            raise GeometryError("Concave or degenerate OBJ quad requires a verified triangulation backend")
                        faces.append([indices[0], indices[2], indices[3]])
                elif parts[0] not in {"o", "g", "s"}:
                    ignored.add(parts[0])
                if len(vertices) > MAX_TRIANGLES * 3 or len(faces) > MAX_TRIANGLES:
                    raise GeometryError("OBJ exceeds the geometry complexity limit")
    except UnicodeError as exc:
        raise GeometryError("OBJ must contain valid UTF-8 text") from exc
    if not faces:
        raise GeometryError("OBJ has no supported triangle or planar-quad faces")
    warnings = ["OBJ material, texture, normal and grouping records are not preserved"] if ignored else []
    return _validated_mesh(Mesh(vertices, faces, source_polygons=polygon_count, warnings=warnings))


_STL_TRIANGLE = struct.Struct("<12fH")


def read_stl(path: Path) -> Mesh:
    """Read binary or ASCII STL while retaining its unindexed source record counts."""
    data = _regular_source(path).read_bytes()
    if len(data) >= 84:
        count = struct.unpack_from("<I", data, 80)[0]
        if count <= MAX_TRIANGLES and len(data) == 84 + 50 * count:
            if count == 0:
                raise GeometryError("STL contains no triangles")
            vertices: list[list[float]] = []
            faces: list[list[int]] = []
            for i in range(count):
                fields = _STL_TRIANGLE.unpack_from(data, 84 + i * 50)
                start = len(vertices)
                vertices.extend([list(fields[3:6]), list(fields[6:9]), list(fields[9:12])])
                faces.append([start, start + 1, start + 2])
            return _validated_mesh(Mesh(vertices, faces))
    try:
        content = data.decode("ascii")
    except UnicodeDecodeError as exc:
        raise GeometryError("Invalid binary or ASCII STL file") from exc
    vertices, faces = [], []
    facet_vertices: list[list[float]] = []
    state = "start"
    for line in content.splitlines():
        fields = line.strip().split()
        if not fields:
            continue
        keyword = fields[0].lower()
        if state == "start" and keyword == "solid":
            state = "solid"
        elif state == "solid" and [word.lower() for word in fields[:2]] == ["facet", "normal"] and len(fields) == 5:
            for number in fields[2:]:
                _number(number)
            state = "facet"
        elif state == "facet" and [word.lower() for word in fields] == ["outer", "loop"]:
            state = "loop"
            facet_vertices = []
        elif state == "loop" and keyword == "vertex" and len(fields) == 4 and len(facet_vertices) < 3:
            facet_vertices.append([_number(v) for v in fields[1:]])
        elif state == "loop" and keyword == "endloop" and len(fields) == 1 and len(facet_vertices) == 3:
            state = "endloop"
        elif state == "endloop" and keyword == "endfacet" and len(fields) == 1:
            start = len(vertices)
            vertices.extend(facet_vertices)
            faces.append([start, start + 1, start + 2])
            facet_vertices = []
            state = "solid"
        elif state == "solid" and keyword == "endsolid":
            state = "done"
        else:
            raise GeometryError("Malformed ASCII STL facet structure")
        if len(faces) > MAX_TRIANGLES:
            raise GeometryError("STL exceeds the triangle limit")
    if state != "done" or not faces:
        raise GeometryError("Invalid ASCII STL structure")
    return _validated_mesh(Mesh(vertices, faces))


def _subtract(a: list[float], b: list[float]) -> list[float]:
    return [a[i] - b[i] for i in range(3)]


def _cross(a: list[float], b: list[float]) -> list[float]:
    return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]


def _dot(a: list[float], b: list[float]) -> float:
    return sum(a[i] * b[i] for i in range(3))


def _length(v: list[float]) -> float:
    return math.sqrt(_dot(v, v))


def mesh_summary(mesh: Mesh) -> dict[str, Any]:
    _validated_mesh(mesh)
    uses = Counter(tuple(sorted((face[i], face[(i + 1) % 3]))) for face in mesh.faces for i in range(3))
    bounds = None
    if mesh.vertices:
        bounds = {"min": [min(v[i] for v in mesh.vertices) for i in range(3)],
                  "max": [max(v[i] for v in mesh.vertices) for i in range(3)]}
    area, signed_volume, degenerate = 0.0, 0.0, 0
    for face in mesh.faces:
        a, b, c = (mesh.vertices[i] for i in face)
        normal = _cross(_subtract(b, a), _subtract(c, a))
        double_area = _length(normal)
        area += double_area * 0.5
        signed_volume += _dot(a, _cross(b, c)) / 6.0
        degenerate += double_area <= 1e-15
    boundary = sum(1 for count in uses.values() if count == 1)
    nonmanifold = sum(1 for count in uses.values() if count > 2)
    watertight = bool(mesh.faces) and boundary == 0 and nonmanifold == 0 and degenerate == 0
    distinct = len({tuple(v) for v in mesh.vertices})
    return {
        "representation": "triangle_mesh", "unit": mesh.unit,
        "vertices": len(mesh.vertices), "distinct_coordinate_vertices": distinct,
        "coincident_coordinate_records": len(mesh.vertices) - distinct,
        "edges": len(uses), "polygons": mesh.source_polygons if mesh.source_polygons is not None else len(mesh.faces),
        "triangles": len(mesh.faces), "boundary_edges": boundary, "nonmanifold_edges": nonmanifold,
        "degenerate_faces": degenerate, "watertight": watertight,
        "bounds": bounds, "surface_area": area,
        "volume": abs(signed_volume) if watertight and signed_volume > 0 else None,
        "warnings": mesh.warnings + (["Mesh is open or non-manifold; solid volume is unavailable"] if not watertight else []),
        "count_semantics": "Edges use source vertex indices; coincident coordinates are not implicitly welded",
    }


def _spatial():
    try:
        from src.utils.sgeo import SGEODocument
        from src.utils.sdrw import SDRWDocument
    except ImportError as exc:
        raise GeometryError("SLAI spatial infrastructure is unavailable; set SLAI_ROOT or use the installed SLAI checkout") from exc
    return SGEODocument, SDRWDocument


def _scene(path: Path, unit: str = "mm", policy: str = "best_effort", dwg_executable: str | None = None):
    SGEODocument, SDRWDocument = _spatial()
    ext = path.suffix.lower()
    if ext == ".sgeo":
        return "3d", SGEODocument.open(path)
    if ext == ".glb":
        return "3d", SGEODocument.import_glb(path, policy=policy)[0]
    if ext in {".stl", ".obj"}:
        mesh = read_obj(path) if ext == ".obj" else read_stl(path)
        scene = SGEODocument.new(path.stem, unit=unit)
        scene.add_mesh(mesh.vertices, mesh.faces, name=path.stem)
        return "3d", scene
    if ext == ".ifc":
        return "3d", SGEODocument.import_ifc(path, policy=policy)[0]
    if ext == ".sdrw":
        return "2d", SDRWDocument.open(path)
    if ext == ".dxf":
        return "2d", SDRWDocument.import_dxf(path, unit=unit, policy=policy)[0]
    if ext == ".svg":
        return "2d", SDRWDocument.import_svg(path, unit=unit, policy=policy)[0]
    if ext == ".dwg":
        if not dwg_executable:
            raise GeometryError("DWG requires a configured ODA File Converter executable")
        return "2d", SDRWDocument.import_dwg(path, executable=dwg_executable, unit=unit, policy=policy)[0]
    raise GeometryError(f"No verified geometry reader for {ext or 'this file'}")


def load_mesh(path: Path, *, unit: str = "mm") -> Mesh:
    unit = _length_unit(unit)
    ext = path.suffix.lower()
    if ext == ".obj":
        mesh = read_obj(path)
        mesh.unit = unit  # OBJ does not define an engineering length unit
        return mesh
    if ext == ".stl":
        mesh = read_stl(path)
        mesh.unit = unit  # user-declared: STL itself has no units
        return mesh
    kind, scene = _scene(path, unit=unit)
    if kind != "3d":
        raise GeometryError("A 2D drawing cannot be treated as a triangle mesh")
    geometry = scene.to_mesh()
    return _validated_mesh(Mesh(geometry.get("vertices", []), geometry.get("faces", []),
                                unit=getattr(scene, "unit", None)))


def inspect(path: Path, *, unit: str = "mm", dwg_executable: str | None = None) -> dict[str, Any]:
    unit = _length_unit(unit)
    path = _regular_source(Path(path))
    ext = path.suffix.lower()
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    if ext in THREE_D:
        result = mesh_summary(load_mesh(path, unit=unit))
        result["measurement_basis"] = (
            "Authored source mesh records" if ext in {".obj", ".stl"}
            else "Tessellation extracted through SLAI; not an exact B-rep topology count"
        )
    elif ext in TWO_D:
        kind, doc = _scene(path, unit=unit, dwg_executable=dwg_executable)
        drawing = doc.snapshot()
        counts = dict(Counter(e.get("kind", "unknown") for e in drawing.entities))
        result = {"representation": "drawing_2d", "unit": drawing.unit,
                  "entities": len(drawing.entities), "entity_counts": counts,
                  "layers": len(drawing.layers), "blocks": len(drawing.blocks),
                  "vertices": None, "edges": None, "polygons": None,
                  "bounds": doc.bounds(), "warnings": [],
                  "measurement_basis": "Source-compatible drawing entities, not triangulated geometry"}
    else:
        raise GeometryError(f"Inspection is not implemented for {ext or 'this file'}")
    return {"source": {"name": path.name, "format": ext.lstrip("."), "size_bytes": path.stat().st_size,
                       "sha256": sha}, "geometry_summary": result}


def _safe_write(path: Path, content: bytes) -> None:
    from applications.tools.storage import atomic_write
    atomic_write(path, content, exclusive=True)


def _obj_bytes(mesh: Mesh) -> bytes:
    lines = ["# CADCon geometry-only OBJ export; materials and semantics are not preserved\n"]
    for x, y, z in mesh.vertices:
        lines.append(f"v {x:.12g} {y:.12g} {z:.12g}\n")
    for a, b, c in mesh.faces:
        lines.append(f"f {a+1} {b+1} {c+1}\n")
    return "".join(lines).encode("utf-8")


def convert(source: Path, destination: Path, *, policy: str = "best_effort", unit: str = "mm",
            dwg_executable: str | None = None) -> dict[str, Any]:
    started = time.perf_counter()
    unit = _length_unit(unit)
    source = _regular_source(Path(source))
    destination = Path(destination)
    if policy not in POLICIES:
        raise GeometryError("Unknown conversion policy")
    if destination.exists():
        raise GeometryError("Destination already exists; CADCon never overwrites without confirmation")
    if source.resolve() == destination.resolve():
        raise GeometryError("Source and target must be different files")
    src, dst = source.suffix.lower(), destination.suffix.lower()
    if src in THREE_D and dst not in NATIVE_3D_OUTPUT or src in TWO_D and dst not in NATIVE_2D_OUTPUT:
        raise GeometryError("No verified conversion path for this 2D/3D format pair")
    notes: list[str] = []
    if src == dst:
        raise GeometryError("Select a different target format; a same-format copy is not a conversion")
    if policy in {"strict", "lossless_where_supported"}:
        if src == ".obj" and read_obj(source).warnings:
            raise GeometryError("Source OBJ has materials, normals or other records not preserved by the CADCon mesh adapter")
        if (src, dst) in {(".obj", ".stl"), (".stl", ".obj")}:
            raise GeometryError("Indexed topology or STL facet-normal records cannot be preserved in this strict exchange")
    before = inspect(source, unit=unit, dwg_executable=dwg_executable)
    backend_report: dict[str, Any] | None = None
    if dst == ".obj":
        if policy in {"strict", "lossless_where_supported"} and src not in {".stl", ".obj"}:
            raise GeometryError("OBJ discards source-specific hierarchy and semantic information under the selected policy")
        mesh = load_mesh(source, unit=unit)
        if mesh.unit and mesh.unit != unit:
            from src.utils.spatial_formats.common.units import convert_length
            factor = convert_length(1.0, mesh.unit, unit)
            mesh.vertices = [[value * factor for value in vertex] for vertex in mesh.vertices]
            notes.append(f"OBJ coordinates expressed in declared {unit} units; original source uses {mesh.unit}")
        _safe_write(destination, _obj_bytes(mesh))
        notes.append("OBJ is geometry-only; materials, UVs, source hierarchy and semantics may be discarded")
    else:
        kind, document = _scene(source, unit=unit, policy=policy, dwg_executable=dwg_executable)
        if kind == "3d":
            if dst == ".sgeo":
                # Keep SGEO's authoritative package serializer, but publish
                # through SLAI's application storage layer. On Windows some
                # AppData filesystems reject the spatial writer's hard-link /
                # no-clobber rename even for a new UUID output filename.
                # Shared atomic_write(exclusive=True) reserves the destination
                # and handles that Windows failure without replacing user data.
                _safe_write(destination, document.dumps())
            elif dst == ".stl":
                if document.unit != unit:
                    if src == ".sgeo":
                        SGEODocument, _ = _spatial()
                        document = SGEODocument.open(source, mode="write")
                    document.convert_unit(unit)
                    notes.append(f"STL has no unit field; output coordinates are expressed in declared {unit} units")
                report = document.export_stl(destination, policy=policy)
                notes.extend(report.dropped + report.approximations + report.warnings)
                backend_report = report.as_dict()
            elif dst == ".glb":
                report = document.export_glb(destination, policy=policy)
                notes.extend(report.dropped + report.approximations + report.warnings)
                backend_report = report.as_dict()
            else:
                raise GeometryError("Unsupported 3D output")
        else:
            if dst == ".sdrw":
                _safe_write(destination, document.dumps())
            elif dst == ".svg":
                report = document.export_svg(destination, policy=policy)
                notes.extend(report.dropped + report.approximations + report.warnings)
                backend_report = report.as_dict()
            elif dst == ".dxf":
                report = document.export_dxf(destination, policy=policy)
                notes.extend(report.dropped + report.approximations + report.warnings)
                backend_report = report.as_dict()
            elif dst == ".dwg" and dwg_executable:
                report = document.export_dwg(destination, executable=dwg_executable, policy=policy)
                notes.extend(report.dropped + report.approximations + report.warnings)
                backend_report = report.as_dict()
            else:
                raise GeometryError("Unsupported 2D output or missing DWG backend")
    try:
        target = inspect(destination, unit=unit, dwg_executable=dwg_executable)
    except Exception:
        destination.unlink(missing_ok=True)  # only the newly written, unverified target
        raise
    return {"status": "completed", "source": source.name, "target": destination.name,
            "source_format": src.lstrip("."), "target_format": dst.lstrip("."),
            "source_sha256": before["source"]["sha256"], "source_summary": before["geometry_summary"],
            "backend": backend_report["adapter"] if backend_report else "SLAI native document / CADCon OBJ",
            "backend_report": backend_report, "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            "unit_assumption": unit, "policy": policy,
            "target_sha256": target["source"]["sha256"], "target_summary": target["geometry_summary"],
            "warnings": sorted(set(notes)), "validated": True}


def compare(first: Path, second: Path, *, unit: str = "mm", tolerance: float = 1e-6) -> dict[str, Any]:
    if not math.isfinite(tolerance) or not 0 <= tolerance <= 1:
        raise GeometryError("Comparison tolerance must be finite and between 0 and 1")
    a, b = inspect(first, unit=unit), inspect(second, unit=unit)
    ga, gb = a["geometry_summary"], b["geometry_summary"]
    if ga["representation"] != gb["representation"]:
        return {"status": "incomparable", "reason": "Different geometric representations",
                "left": a, "right": b, "differences": {}}
    changes = {}
    for key in ("vertices", "edges", "polygons", "triangles", "entities", "layers", "blocks", "watertight"):
        if key in ga and key in gb and ga[key] != gb[key]:
            changes[key] = {"left": ga[key], "right": gb[key]}
    for key in ("surface_area", "volume"):
        x, y = ga.get(key), gb.get(key)
        if x is not None and y is not None and not math.isclose(x, y, abs_tol=tolerance, rel_tol=tolerance):
            changes[key] = {"left": x, "right": y}
    bounds_a, bounds_b = ga.get("bounds"), gb.get("bounds")
    if not _bounds_close(bounds_a, bounds_b, tolerance):
        changes["bounds"] = {"left": ga.get("bounds"), "right": gb.get("bounds")}
    same_bytes = a["source"]["sha256"] == b["source"]["sha256"]
    # Compare triangles by their actual world positions, not counts alone. Face
    # order and vertex records may differ across loss-aware serialization.
    if same_bytes:
        status = "identical"
    elif ga["representation"] == "triangle_mesh":
        left, right = load_mesh(first, unit=unit), load_mesh(second, unit=unit)
        status = "geometrically_equivalent" if _same_triangles(left, right, tolerance) else "modified"
    else:
        status = "modified" if changes else "analysis_incomplete"
    return {"status": status, "left": a, "right": b, "differences": changes,
            "tolerance": tolerance, "note": "Triangle equivalence is topology-sensitive; surface-distance registration remains unavailable"}


def _bounds_close(first, second, tolerance: float) -> bool:
    if first is None or second is None:
        return first is second
    try:
        return all(math.isclose(float(a), float(b), abs_tol=tolerance, rel_tol=tolerance)
                   for side in ("min", "max") for a, b in zip(first[side], second[side]))
    except (KeyError, ValueError, TypeError):
        return first == second


def _same_triangles(first: Mesh, second: Mesh, tolerance: float) -> bool:
    if len(first.faces) != len(second.faces):
        return False
    # Hashing quantized coordinates avoids an O(N*M) all-pairs search. This is
    # a topological/sampled equivalence test, not a Hausdorff-distance proof.
    from src.utils.spatial_formats.common.units import convert_length
    first_scale = convert_length(1.0, first.unit, "m") if first.unit else 1.0
    second_scale = convert_length(1.0, second.unit, "m") if second.unit else 1.0
    if tolerance == 0:
        quantize = lambda v: tuple(v)
    else:
        quantize = lambda v: tuple(round(x / tolerance) for x in v)
    def canonical(mesh, factor):
        return sorted(tuple(sorted(quantize([x * factor for x in mesh.vertices[i]]) for i in face)) for face in mesh.faces)
    return canonical(first, first_scale) == canonical(second, second_scale)


def analysis(path: Path, *, unit: str = "mm") -> dict[str, Any]:
    result = inspect(path, unit=unit)
    geometry = result["geometry_summary"]
    findings: list[dict[str, Any]] = []
    if geometry["representation"] == "triangle_mesh":
        checks = (("boundary_edges", "cadcon.mesh.open_boundaries", "warning", "Mesh has open boundary edges",
                   "Check for gaps or deliberately open surfaces before interpreting solid volume"),
                  ("nonmanifold_edges", "cadcon.mesh.nonmanifold", "error", "Mesh contains non-manifold edges",
                   "Repair or review edges used by more than two faces"),
                  ("degenerate_faces", "cadcon.mesh.degenerate_faces", "error", "Mesh contains zero-area triangles",
                   "Remove or reconstruct degenerate faces"))
        for key, code, severity, description, recommendation in checks:
            count = geometry.get(key, 0)
            if count:
                findings.append({"code": code, "severity": severity, "description": description,
                                 "measured_value": count, "expected_condition": 0,
                                 "evidence": {"check": key, "count_semantics": geometry["count_semantics"]},
                                 "recommendation": recommendation})
    validation = {
        "executed": ["indexed-edge-incidence", "degenerate-triangle-area", "finite-coordinates"]
        if geometry["representation"] == "triangle_mesh" else ["drawing-entity-extraction"],
        "not_executed": ["self-intersection", "rigorous B-rep validity", "global face-orientation consistency"]
        if geometry["representation"] == "triangle_mesh" else ["mesh topology", "solid volume"],
    }
    return {"report_kind": "cadcon.analysis", "report_version": "1.0.0",
            "generated_at": datetime.now(timezone.utc).isoformat(), **result,
            "findings": findings, "validation": validation,
            "provenance": {"source_sha256": result["source"]["sha256"]}}


def spatial_nearest(path: Path, point_m: list[float], *, unit: str = "mm") -> dict[str, Any]:
    """Real SLAI Spatial subsystem nearest-entity query, opt-in and isolated.

    The Spatial Agent's shared subsystem owns indexing and distance semantics;
    CADCon only adapts a revisioned SGEO/SDRW document into agent DTOs.
    """
    if not isinstance(point_m, list) or len(point_m) != 3 or not all(
        isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
        for value in point_m
    ):
        raise GeometryError("Spatial query point must contain three finite metre coordinates")
    kind, document = _scene(path, unit=unit)
    if kind == "3d":
        bundle = document.to_agent_bundle(unit="m")
        reference = document.reference(uri="cadcon:current-model")
    else:
        from src.utils.spatial_formats.adapters.slai_spatial import to_drawing_bundle
        bundle = to_drawing_bundle(document.snapshot(), unit="m")
        reference = {"document_id": document.document_id, "revision": document.revision,
                     "format_id": "slai.sdrw", "units": document.unit}
    from src.agents.spatial import SpatialMemory, SpatialQueries
    memory = SpatialMemory()
    for entity in bundle.entities:
        memory.upsert_entity(entity)
    query = SpatialQueries(memory=memory).nearest(tuple(float(x) for x in point_m), frame_id="world")
    return {"backend": "src.agents.spatial.SpatialQueries", "units": "m",
            "reference": reference, "query": query.to_dict(), "entity_count": len(bundle.entities),
            "meaning": "Nearest spatial-entity centre; not distance to the exact solid surface"}


def report_bytes(data: dict[str, Any], fmt: str) -> bytes:
    if fmt == "json":
        return json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if fmt != "sldc":
        raise GeometryError("Report format must be JSON or SLDC")
    from src.utils import sldc
    is_comparison = data.get("report_kind") == "cadcon.comparison"
    title = "CADCon Model Comparison" if is_comparison else "CADCon Geometry Analysis"
    generated = data.get("generated_at") or datetime.now(timezone.utc).isoformat()
    blocks = sldc.blocks_from_text(f"# {title}\n\nCADCon v1.0 · Generated {generated}\n", markdown=True)

    def value(item) -> str:
        if item is None:
            return "Not available"
        if isinstance(item, (dict, list, tuple)):
            return json.dumps(item, ensure_ascii=False, allow_nan=False)
        return str(item)

    def section(name: str, values: dict[str, Any]) -> None:
        blocks.extend(sldc.blocks_from_text("## " + name, markdown=True))
        rows = [["Metric", "Value"]] + [[key.replace("_", " ").capitalize(), value(item)] for key, item in values.items()]
        blocks.append(sldc.make_table(rows, header_rows=1, caption=name))

    if is_comparison:
        section("Overview", {"status": data.get("status"), "tolerance": data.get("tolerance"),
                             "limitations": data.get("note") or data.get("reason")})
        for key in ("left", "right"):
            source = data.get(key) or {}
            section(key.title() + " file", source.get("source", {}))
            section(key.title() + " geometry", source.get("geometry_summary", {}))
        differences = data.get("differences") or {}
        section("Differences", {key: f"{value(pair.get('left'))} → {value(pair.get('right'))}"
                                for key, pair in differences.items()})
    else:
        section("Source", data.get("source", {}))
        section("Geometry summary", data.get("geometry_summary", {}))
        section("Validation coverage", data.get("validation", {}))
        findings = data.get("findings", [])
        section("Findings", {str(i + 1): f"[{finding['severity']}] {finding['description']}"
                             for i, finding in enumerate(findings)} or {"status": "No warnings recorded"})
        section("Provenance", data.get("provenance", {}))
    # blocks_from_text numbers each independently parsed section from b00001.
    # Let the authoritative document builder assign globally unique block IDs.
    for block in blocks:
        block.pop("id", None)
    package = sldc.new_package(title, blocks)
    return sldc.dumps_package(package)


def capabilities(*, dwg_executable: str | None = None) -> dict[str, Any]:
    native = {"stl": True, "obj": True, "sgeo": False, "sdrw": False, "glb": False,
              "dxf": False, "svg": False, "dwg": False, "ifc": False}
    details = {}
    spatial = False
    try:
        from src.utils.sgeo import capabilities as spatial_capabilities
        from src.utils.sdrw import SDRWDocument  # noqa: F401 - availability probe
        spatial = True
        native.update({"sgeo": True, "sdrw": True, "glb": True, "svg": True})
        details = spatial_capabilities(dwg_executable=dwg_executable)
        for name in ("dxf", "dwg", "ifc"):
            native[name] = bool(details.get(name, {}).get("available"))
    except ImportError:
        pass
    imported = {"stl": "CADCon ASCII/binary STL", "obj": "CADCon Wavefront geometry",
                "sgeo": "SLAI SGEO", "sdrw": "SLAI SDRW", "glb": "SLAI GLB adapter",
                "dxf": "SLAI DXF / ezdxf", "svg": "SLAI SVG adapter",
                "dwg": "SLAI DWG / configured ODA", "ifc": "SLAI IFC / IfcOpenShell"}
    routes: dict[str, list[str]] = {}
    for name, available in native.items():
        if not available:
            routes[name] = []
        elif name in {"stl", "obj", "sgeo", "glb", "ifc"}:
            targets = {"obj"} | ({"sgeo", "stl", "glb"} if spatial else set())
            routes[name] = sorted(target for target in targets if target != name and native.get(target))
        else:
            routes[name] = sorted(target for target in ("sdrw", "svg", "dxf", "dwg")
                                  if target != name and native.get(target))
    return {"formats": {name: {"available": available, "import": available,
                                "export": available and name != "ifc" and (spatial or name == "obj"),
                                "backend": imported[name],
                                "limitations": details.get(name, {}).get("limitations", [])}
                        for name, available in native.items()},
            "routes": routes,
            "conditional": {name.lstrip("."): "No verified CADCon conversion backend" for name in sorted(OPTIONAL_FORMATS)},
            "spatial_adapters": details,
            "policy_options": list(POLICIES)}
