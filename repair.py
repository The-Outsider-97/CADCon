"""Deterministic, bounded mesh diagnosis and conservative repair candidates.

This is not an alternative SGEO serializer: candidates are ordinary CADCon
meshes and are exported through SLAI's authoritative SGEODocument API.
"""
from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any

try:
    from .engine import GeometryError, Mesh, _cross, _dot, _length, _subtract, _validated_mesh, mesh_summary
except ImportError:  # Direct run_cadcon.py launcher
    from engine import GeometryError, Mesh, _cross, _dot, _length, _subtract, _validated_mesh, mesh_summary

MAX_PATCH_EDGES = 128
MAX_DIAGNOSTIC_LOOPS = 256
# A bounded search, from nearly exact stitching to at most 0.1% of the
# model diagonal.  Larger values remain an explicit user decision.
AUTO_WELD_FRACTIONS = (1e-7, 1e-6, 1e-5, 1e-4, 1e-3)


@dataclass
class Candidate:
    strategy: str
    label: str
    mesh: Mesh
    added_faces: list[int]
    explanation: str
    source_face_count: int
    validation: dict[str, Any]

    def details(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy, "label": self.label,
            "explanation": self.explanation, "added_faces": len(self.added_faces),
            "modified_faces": 0, "removed_faces": 0,
            "source_face_count": self.source_face_count,
            "validation": self.validation,
            "face_provenance": "Original face indices are preserved; added faces are appended",
        }


def weld(mesh: Mesh, tolerance: float = 0.0) -> Mesh:
    """Build shared indexed topology; never move a source coordinate."""
    _validated_mesh(mesh)
    if not isinstance(tolerance, (float, int)) or not math.isfinite(tolerance) or tolerance < 0:
        raise GeometryError("Welding tolerance must be a finite non-negative number")
    if mesh.vertices:
        extents = [max(v[i] for v in mesh.vertices) - min(v[i] for v in mesh.vertices) for i in range(3)]
        diagonal = math.sqrt(sum(x * x for x in extents))
        if tolerance and tolerance > max(diagonal * .01, 1e-9):
            raise GeometryError("Welding tolerance exceeds 1% of the model diagonal")
    vertices: list[list[float]] = []
    remap: list[int] = []
    if tolerance == 0:
        lookup: dict[tuple[float, float, float], int] = {}
        for vertex in mesh.vertices:
            key = tuple(vertex)
            if key not in lookup:
                lookup[key] = len(vertices)
                vertices.append(list(vertex))
            remap.append(lookup[key])
    else:
        cells: dict[tuple[int, int, int], list[int]] = defaultdict(list)
        maximum_distance = tolerance * tolerance
        for vertex in mesh.vertices:
            cell = tuple(math.floor(coordinate / tolerance) for coordinate in vertex)
            closest: tuple[float, int] | None = None
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for dz in (-1, 0, 1):
                        for idx in cells.get((cell[0] + dx, cell[1] + dy, cell[2] + dz), ()):
                            distance = sum((a - b) ** 2 for a, b in zip(vertex, vertices[idx]))
                            if distance <= maximum_distance and (closest is None or (distance, idx) < closest):
                                closest = (distance, idx)
            if closest is None:
                index = len(vertices)
                vertices.append(list(vertex))
                cells[cell].append(index)
            else:
                index = closest[1]
            remap.append(index)
    faces = [[remap[i] for i in face] for face in mesh.faces]
    if any(len(set(face)) != 3 for face in faces):
        raise GeometryError("Welding would collapse one or more triangles; reduce the tolerance")
    return _validated_mesh(Mesh(vertices, faces, unit=mesh.unit,
                                source_polygons=mesh.source_polygons, warnings=list(mesh.warnings)))


def _edges(mesh: Mesh) -> dict[tuple[int, int], list[tuple[int, int, int]]]:
    edges: dict[tuple[int, int], list[tuple[int, int, int]]] = defaultdict(list)
    for number, (a, b, c) in enumerate(mesh.faces):
        for start, end in ((a, b), (b, c), (c, a)):
            edges[(min(start, end), max(start, end))].append((number, start, end))
    return edges


def _components(mesh: Mesh, edges: dict) -> tuple[list[int], int]:
    parent = list(range(len(mesh.faces)))

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for uses in edges.values():
        if len(uses) == 2:
            a, b = root(uses[0][0]), root(uses[1][0])
            if a != b:
                parent[max(a, b)] = min(a, b)
    ids = [root(i) for i in range(len(mesh.faces))]
    lookup = {key: idx for idx, key in enumerate(sorted(set(ids)))}
    return [lookup[i] for i in ids], len(lookup)


def _boundary_loops(edges: dict) -> tuple[list[list[int]], int]:
    directed = [(uses[0][1], uses[0][2]) for uses in edges.values() if len(uses) == 1]
    next_edges: dict[int, list[int]] = defaultdict(list)
    entering = Counter()
    for a, b in directed:
        next_edges[a].append(b)
        entering[b] += 1
    seen: set[tuple[int, int]] = set()
    loops: list[list[int]] = []
    open_chains = 0
    for start, end in directed:
        if (start, end) in seen:
            continue
        loop = [start]
        a, b = start, end
        while (a, b) not in seen:
            seen.add((a, b))
            loop.append(b)
            if b == start:
                if len(loop) > 3:
                    loops.append(loop[:-1])
                break
            if len(next_edges[b]) != 1 or entering[b] != 1:
                open_chains += 1
                break
            a, b = b, next_edges[b][0]
        else:
            open_chains += 1
    return loops, open_chains


def validate(mesh: Mesh) -> dict[str, Any]:
    summary = mesh_summary(mesh)
    edges = _edges(mesh)
    inconsistent = sum(1 for uses in edges.values()
                       if len(uses) == 2 and uses[0][1:] == uses[1][1:])
    _, components = _components(mesh, edges)
    closed = bool(mesh.faces) and not (summary["boundary_edges"] or summary["nonmanifold_edges"]
                                       or summary["degenerate_faces"])
    oriented = inconsistent == 0
    loops, chains = _boundary_loops(edges)
    return {
        "vertices": summary["vertices"], "edges": summary["edges"],
        "triangles": summary["triangles"], "boundary_edges": summary["boundary_edges"],
        "nonmanifold_edges": summary["nonmanifold_edges"],
        "degenerate_faces": summary["degenerate_faces"],
        "inconsistent_winding_edges": inconsistent, "manifold_face_components": components,
        "boundary_loops": len(loops), "open_chains": chains,
        "topologically_closed": closed, "consistent_winding": oriented,
        "watertight": closed and oriented,
        "solid_validity": "not_verified_self_intersections_or_shell_overlaps",
        "volume": None,  # Not a verified solid unless stronger checks run.
        "surface_area": summary["surface_area"], "bounds": summary["bounds"],
        "status": "topologically_closed_geometry_unverified" if closed and oriented
                  else "still_open_or_nonmanifold",
    }


def recommend_tolerance(mesh: Mesh) -> dict[str, Any]:
    """Find a conservative, evidence-backed weld tolerance for an input mesh.

    Only consider tolerance welding if exact matching leaves actual boundary
    edges.  Reject levels that collapse faces or create non-manifold edges;
    never mistake a missing surface for a small coordinate discrepancy.
    """
    exact = weld(mesh)
    baseline = validate(exact)
    diagonal = _length([max(vertex[axis] for vertex in mesh.vertices) -
                        min(vertex[axis] for vertex in mesh.vertices) for axis in range(3)]) if mesh.vertices else 0.0
    recommendation = {
        "tolerance": 0.0, "unit": mesh.unit,
        "model_diagonal": diagonal, "evaluated_levels": 0,
        "baseline_boundary_edges": baseline["boundary_edges"],
        "expected_boundary_edges": baseline["boundary_edges"],
        "expected_nonmanifold_edges": baseline["nonmanifold_edges"],
        "reason": "Exact-coordinate reconstruction is the safest starting point.",
    }
    if not baseline["boundary_edges"]:
        recommendation["reason"] = (
            "Exact welding already removes all indexed boundary edges. A larger tolerance "
            "would not close additional holes and could merge intentional features."
        )
        return recommendation
    if diagonal <= 0:
        recommendation["reason"] = "Model dimensions do not permit a meaningful automatic tolerance."
        return recommendation

    best = baseline
    for fraction in AUTO_WELD_FRACTIONS:
        tolerance = diagonal * fraction
        recommendation["evaluated_levels"] += 1
        try:
            checks = validate(weld(mesh, tolerance))
        except GeometryError:
            # Collapsed triangles or another invalid remap: never recommend it.
            continue
        if (checks["nonmanifold_edges"] > baseline["nonmanifold_edges"] or
                checks["degenerate_faces"] > baseline["degenerate_faces"] or
                checks["inconsistent_winding_edges"] > baseline["inconsistent_winding_edges"]):
            continue
        if checks["boundary_edges"] < best["boundary_edges"]:
            best = checks
            recommendation.update(tolerance=tolerance,
                                  expected_boundary_edges=checks["boundary_edges"],
                                  expected_nonmanifold_edges=checks["nonmanifold_edges"])
            if not checks["boundary_edges"]:
                break
    if recommendation["tolerance"]:
        recommendation["reason"] = (
            "A model-scaled tolerance reduced reconstructed boundary edges without "
            "introducing new non-manifold or degenerate edges in the checks performed. "
            "Review intentional seams before exporting."
        )
    else:
        recommendation["reason"] = (
            "No safe automatic welding level improved the boundary topology. "
            "The model may contain genuine holes or intentional openings; inspect repair patches."
        )
    return recommendation


def diagnose(mesh: Mesh, tolerance: float = 0.0) -> dict[str, Any]:
    original = validate(mesh)
    reconstructed = weld(mesh, tolerance)
    updated = validate(reconstructed)
    loops, chains = _boundary_loops(_edges(reconstructed))
    sample = []
    for loop in loops[:MAX_DIAGNOSTIC_LOOPS]:
        length = sum(_length(_subtract(reconstructed.vertices[a], reconstructed.vertices[b]))
                     for a, b in zip(loop, loop[1:] + loop[:1]))
        sample.append({"edges": len(loop), "perimeter": length})
    return {
        "source": original, "reconstructed": updated,
        "tolerance": tolerance, "unit": mesh.unit,
        "coincident_records": len(mesh.vertices) - len({tuple(v) for v in mesh.vertices}),
        "boundary_loops": sample, "loops_truncated": len(loops) > len(sample),
        "open_chains": chains,
        "interpretation": ("The indexed boundary count was an artifact of duplicated coordinates; "
                           "reconstructed topology still has non-manifold edges."
                           if original["boundary_edges"] and not updated["boundary_edges"] and updated["nonmanifold_edges"]
                           else "Topology has been reconstructed; review remaining boundaries and shell validity."),
        "validation_limitations": ["Self-intersection and shell-overlap tests were not executed",
                                   "Topological closure does not establish a valid CAD solid"],
    }


def _isolate_shells(mesh: Mesh) -> Mesh | None:
    edges = _edges(mesh)
    membership, components = _components(mesh, edges)
    if components < 2 or not any(len(uses) > 2 for uses in edges.values()):
        return None
    vertices: list[list[float]] = []
    faces: list[list[int]] = []
    remap: dict[tuple[int, int], int] = {}
    for index, face in enumerate(mesh.faces):
        out = []
        for vertex in face:
            key = (membership[index], vertex)
            if key not in remap:
                remap[key] = len(vertices)
                vertices.append(list(mesh.vertices[vertex]))
            out.append(remap[key])
        faces.append(out)
    candidate = _validated_mesh(Mesh(vertices, faces, unit=mesh.unit))
    if validate(candidate)["nonmanifold_edges"] >= validate(mesh)["nonmanifold_edges"]:
        return None
    return candidate


def _planar_points(mesh: Mesh, loop: list[int]) -> tuple[list[tuple[float, float]], float] | None:
    if not 3 <= len(loop) <= MAX_PATCH_EDGES:
        return None
    points = [mesh.vertices[i] for i in loop]
    normal = [0.0, 0.0, 0.0]
    for a, b in zip(points, points[1:] + points[:1]):
        normal = [normal[0] + (a[1] - b[1]) * (a[2] + b[2]),
                  normal[1] + (a[2] - b[2]) * (a[0] + b[0]),
                  normal[2] + (a[0] - b[0]) * (a[1] + b[1])]
    magnitude = _length(normal)
    if magnitude <= 1e-14:
        return None
    direction = [x / magnitude for x in normal]
    span = max(max(p[i] for p in points) - min(p[i] for p in points) for i in range(3))
    if any(abs(_dot(_subtract(p, points[0]), direction)) > max(span * 1e-5, 1e-9) for p in points):
        return None
    axis = max(range(3), key=lambda i: abs(direction[i]))
    keep = [i for i in range(3) if i != axis]
    return [(p[keep[0]], p[keep[1]]) for p in points], magnitude


def _ear_clip(loop: list[int], coords: list[tuple[float, float]]) -> list[list[int]] | None:
    area = sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(coords, coords[1:] + coords[:1]))
    if abs(area) < 1e-15:
        return None
    sign = 1 if area > 0 else -1
    remaining = list(range(len(loop)))
    triangles: list[list[int]] = []

    def turn(i: int, j: int, k: int) -> float:
        a, b, c = coords[i], coords[j], coords[k]
        return (b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0])

    while len(remaining) > 3:
        found = False
        for i in range(len(remaining)):
            a, b, c = (remaining[(i-1) % len(remaining)], remaining[i],
                       remaining[(i+1) % len(remaining)])
            if sign * turn(a, b, c) <= 1e-14:
                continue
            if any(sign * turn(a, b, p) >= -1e-12 and
                   sign * turn(b, c, p) >= -1e-12 and
                   sign * turn(c, a, p) >= -1e-12
                   for p in remaining if p not in (a, b, c)):
                continue
            triangles.append([loop[a], loop[b], loop[c]])
            remaining.pop(i)
            found = True
            break
        if not found:
            return None
    triangles.append([loop[i] for i in remaining])
    return triangles


def _fill_planar(mesh: Mesh, *, fan: bool) -> tuple[Mesh, list[int]] | None:
    loops, open_chains = _boundary_loops(_edges(mesh))
    if open_chains or not loops or len(loops) > MAX_DIAGNOSTIC_LOOPS:
        return None
    vertices = [list(v) for v in mesh.vertices]
    faces = [list(f) for f in mesh.faces]
    appended = []
    for loop in loops:
        planar = _planar_points(mesh, loop)
        if planar is None:
            return None
        coords, _ = planar
        # Repair faces must oppose the existing directed boundary edge.
        reversed_loop, reversed_coords = list(reversed(loop)), list(reversed(coords))
        if fan:
            # The centroid fan is safe for convex loops only.
            turns = []
            for i in range(len(coords)):
                a, b, c = coords[i-1], coords[i], coords[(i+1) % len(coords)]
                turns.append((b[0]-a[0])*(c[1]-b[1])-(b[1]-a[1])*(c[0]-b[0]))
            if min(turns) < -1e-12 and max(turns) > 1e-12:
                return None
            center = [sum(mesh.vertices[idx][axis] for idx in loop) / len(loop) for axis in range(3)]
            center_idx = len(vertices)
            vertices.append(center)
            patch = [[a, b, center_idx] for a, b in zip(reversed_loop, reversed_loop[1:] + reversed_loop[:1])]
        else:
            patch = _ear_clip(reversed_loop, reversed_coords)
            if patch is None:
                return None
        for face in patch:
            if _length(_cross(_subtract(vertices[face[1]], vertices[face[0]]),
                              _subtract(vertices[face[2]], vertices[face[0]]))) <= 1e-14:
                return None
        appended.extend(range(len(faces), len(faces) + len(patch)))
        faces.extend(patch)
    return _validated_mesh(Mesh(vertices, faces, unit=mesh.unit)), appended


def candidates(mesh: Mesh, tolerance: float = 0.0, limit: int = 5) -> list[Candidate]:
    if not isinstance(limit, int) or not 1 <= limit <= 5:
        raise GeometryError("Choose between one and five repair variants")
    original = validate(mesh)
    base = weld(mesh)
    base_validation = validate(base)
    built: list[Candidate] = []

    def append(strategy: str, label: str, result: Mesh, added: list[int], explanation: str) -> None:
        if len(built) >= 5:
            return
        checks = validate(result)
        # Avoid duplicate candidates and changes that do not improve any defect.
        fingerprint = (checks["boundary_edges"], checks["nonmanifold_edges"],
                       checks["inconsistent_winding_edges"], len(result.vertices), len(result.faces))
        if any((item.validation["boundary_edges"], item.validation["nonmanifold_edges"],
                item.validation["inconsistent_winding_edges"], len(item.mesh.vertices),
                len(item.mesh.faces)) == fingerprint for item in built):
            return
        if (checks["boundary_edges"] > original["boundary_edges"] or
                checks["nonmanifold_edges"] > max(original["nonmanifold_edges"], base_validation["nonmanifold_edges"])):
            return
        built.append(Candidate(strategy, label, result, added, explanation,
                               len(mesh.faces), checks))

    if len(base.vertices) != len(mesh.vertices) or base_validation != original:
        append("topology", "Exact topology reconstruction", base, [],
               "Reindex identical coordinates without adding or moving surface faces.")
    if tolerance > 0:
        approximate = weld(mesh, tolerance)
        if len(approximate.vertices) < len(base.vertices):
            append("tolerant_weld", "Tolerance-based stitching", approximate, [],
                   f"Stitch coordinates within {tolerance:g} {mesh.unit or 'source units'}; this may join intentional seams.")
    isolated = _isolate_shells(base)
    if isolated:
        append("isolate_shells", "Separate touching manifold shells", isolated, [],
               "Split coincident topology by manifold face components; geometric shell overlaps remain unverified.")
    for fan, strategy, label in ((False, "hole_fill", "Local planar hole triangulation"),
                                 (True, "planar_cap", "Planar centroid caps")):
        repaired = _fill_planar(base, fan=fan)
        if repaired:
            result, added = repaired
            append(strategy, label, result, added,
                   "Create red faces on verified planar boundary loops. Intentional openings require user review.")
    built.sort(key=lambda item: (not item.validation["watertight"],
                                 item.validation["nonmanifold_edges"],
                                 item.validation["boundary_edges"], len(item.added_faces),
                                 item.strategy))
    return built[:limit]


def capability_info() -> dict[str, Any]:
    return {
        "available": ["topology", "tolerant_weld", "isolate_shells", "hole_fill", "planar_cap"],
        "conditional": {"tolerant_weld": "Requires explicit positive tolerance",
                        "isolate_shells": "Requires separable manifold face components",
                        "hole_fill": "Requires simple planar boundary loops up to 128 edges",
                        "planar_cap": "Requires convex planar boundary loops up to 128 edges"},
        "unavailable": {"curvature_reconstruction": "No verified curvature-fairing backend configured",
                        "volumetric_reconstruction": "No verified volumetric-remeshing backend configured"},
        "agent_runtime": "unconfigured; deterministic repair remains available",
    }
