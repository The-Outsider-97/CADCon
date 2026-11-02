"""Deterministic, bounded mesh diagnosis and repair candidates.

This is not an alternative SGEO serializer: candidates are ordinary CADCon meshes and are exported through SLAI's authoritative SGEODocument
API. Everything here is deterministic geometry; SLAI agents may *explain* or *review* its output elsewhere but never change it.

Repair policy (``repair.mode`` in cadcon_config.yaml) is real behaviour, not a label:

``conservative``  exact stitching, then welds up to 0.1 % of the model diagonal; a level that collapses any triangle or introduces any defect
                  is rejected; only strict improvements are recommended.
``balanced``      welds up to 0.5 %; tolerance levels also come from the *measured* gaps between boundary vertices; collapsed sliver triangles
                  may be removed (at most 0.5 % of the faces, always reported); a larger tolerance is recommended only when it gains something.
``exploratory``   welds up to 1 % (the hard cap of :func:`weld`); up to 2 % of faces may be removed; every candidate that relied on a level
                  beyond the conservative ceiling or on face removal is labelled ``risk: exploratory`` and is never ``verified_safe``.

In every mode validation is the same independent topology check (:func:`validate`): a candidate that still has boundary edges is a *partial
improvement* and says so, and nothing is ever called a valid solid.
"""
from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Mapping

try:
    from .engine import GeometryError, Mesh, face_components, indexed_edges, _cross, _dot, _length, _subtract, _validated_mesh, mesh_summary
    from .utils.cadcon_helpers import EVIDENCE_KINDS, bounds_diagonal, evidence, format_tolerance, ESTIMATED, MEASURED
except ImportError:  # Direct run_cadcon.py launcher
    from engine import GeometryError, Mesh, face_components, indexed_edges, _cross, _dot, _length, _subtract, _validated_mesh, mesh_summary
    from utils.cadcon_helpers import EVIDENCE_KINDS, bounds_diagonal, evidence, format_tolerance, ESTIMATED, MEASURED

MAX_PATCH_EDGES = 128
MAX_DIAGNOSTIC_LOOPS = 256
HARD_WELD_FRACTION = 0.01          # weld() refuses more than 1 % of the model diagonal in every mode
MAX_GAP_VERTICES = 100_000         # boundary vertices examined when measuring gaps (deterministic stride sample above this)
STRATEGIES = ("topology", "tolerant_weld", "isolate_shells", "hole_fill", "planar_cap")
# Kept for callers written before repair modes existed: the conservative ladder.
AUTO_WELD_FRACTIONS = (1e-7, 1e-6, 1e-5, 1e-4, 1e-3)


@dataclass(frozen=True)
class RepairPolicy:
    mode: str
    ceiling_fraction: float          # largest welding tolerance, as a fraction of the model diagonal
    ladder: tuple[float, ...]        # fixed fractions always evaluated (besides gap-derived levels)
    max_levels: int
    max_removed_fraction: float      # collapsed triangles that may be removed (0 = any collapse rejects the level)
    min_gain_fraction: float         # a larger level must remove at least this fraction of the remaining boundary edges
    use_measured_gaps: bool
    description: str


POLICIES: dict[str, RepairPolicy] = {
    "conservative": RepairPolicy("conservative", 1e-3, AUTO_WELD_FRACTIONS, 6, 0.0, 0.0, False,
                                 "Exact stitching and welds up to 0.1% of the diagonal; any collapse or new defect rejects a level."),
    "balanced": RepairPolicy("balanced", 5e-3, (1e-7, 1e-6, 1e-5, 1e-4, 5e-4, 1e-3, 2e-3, 5e-3), 12, 0.005, 0.02, True,
                             "Welds up to 0.5% of the diagonal using measured boundary gaps; sliver collapses are removed and reported."),
    "exploratory": RepairPolicy("exploratory", HARD_WELD_FRACTION, (1e-7, 1e-6, 1e-5, 1e-4, 5e-4, 1e-3, 2e-3, 5e-3, 1e-2), 14, 0.02, 0.0, True,
                                "Welds up to 1% of the diagonal; results are labelled exploratory and never verified-safe."),
}
CONSERVATIVE_CEILING = POLICIES["conservative"].ceiling_fraction


@dataclass(frozen=True)
class RepairSettings:
    """The ``repair`` section of cadcon_config.yaml (defaults match the shipped file)."""

    mode: str = "balanced"
    max_candidates: int = 5
    large_mesh_triangles: int = 200_000
    large_mesh_max_candidates: int = 2
    max_patch_edges: int = MAX_PATCH_EDGES
    max_diagnostic_loops: int = MAX_DIAGNOSTIC_LOOPS
    max_tolerance_fraction: float | None = None
    enabled_strategies: tuple[str, ...] = STRATEGIES

    @classmethod
    def from_config(cls, config: Any) -> "RepairSettings":
        section = config.section("repair")
        return cls(**{name: section[name] for name in cls.__dataclass_fields__ if name in section})

    @property
    def policy(self) -> RepairPolicy:
        base = POLICIES[self.mode]
        if self.max_tolerance_fraction is not None and self.max_tolerance_fraction < base.ceiling_fraction:
            return RepairPolicy(**{**base.__dict__, "ceiling_fraction": self.max_tolerance_fraction})
        return base

    def candidate_limit(self, triangles: int, requested: int | None = None) -> int:
        limit = self.max_candidates if requested is None else min(requested, self.max_candidates)
        return min(limit, self.large_mesh_max_candidates) if triangles > self.large_mesh_triangles else limit

    def allows(self, strategy: str) -> bool:
        return strategy in self.enabled_strategies


DEFAULT_SETTINGS = RepairSettings()


@dataclass
class Candidate:
    strategy: str
    label: str
    mesh: Mesh
    added_faces: list[int]
    explanation: str
    source_face_count: int
    validation: dict[str, Any]
    tolerance: float = 0.0
    removed_face_indices: list[int] = field(default_factory=list)
    deviation: float | None = None             # largest distance any source vertex moved (measured), None when not applicable
    risk: str = "none_identified"              # "none_identified" | "exploratory"
    mode: str = "balanced"
    notes: list[str] = field(default_factory=list)
    ranking: dict[str, Any] | None = None
    baseline: dict[str, Any] | None = None     # validation of the exactly-welded source, for before/after reporting

    @property
    def repair_status(self) -> str:
        v = self.validation
        if v["watertight"]:
            return "closed_topology_solid_unverified"
        if self.baseline and (v["boundary_edges"] < self.baseline["boundary_edges"] or v["nonmanifold_edges"] < self.baseline["nonmanifold_edges"]):
            return "partial_improvement"
        return "no_measured_improvement"

    def details(self) -> dict[str, Any]:
        out = {
            "strategy": self.strategy, "label": self.label,
            "explanation": self.explanation, "added_faces": len(self.added_faces),
            "modified_faces": 0, "removed_faces": len(self.removed_face_indices),
            "source_face_count": self.source_face_count,
            "validation": self.validation,
            "repair_status": self.repair_status,
            "tolerance": self.tolerance, "unit": self.mesh.unit,
            "deviation": self.deviation, "risk": self.risk, "mode": self.mode,
            "verified_safe": self.risk == "none_identified" and self.validation["watertight"] and not self.removed_face_indices,
            "notes": list(self.notes),
            "face_provenance": ("Original face indices are preserved; added faces are appended"
                                if not self.removed_face_indices else
                                "Faces collapsed by the weld were removed; remaining faces keep their relative order and added faces are appended"),
            "evidence": [evidence(MEASURED, "repair.validate", "Topology counts of this candidate were measured on its own mesh")],
        }
        if self.removed_face_indices:
            out["removed_face_indices"] = self.removed_face_indices[:200]
        if self.baseline:
            out["improvement"] = {key: {"before": self.baseline[key], "after": self.validation[key]}
                                  for key in ("boundary_edges", "nonmanifold_edges", "degenerate_faces", "inconsistent_winding_edges")}
        if self.ranking:
            out["ranking"] = self.ranking
        return out


@dataclass
class WeldReport:
    merged_vertices: int = 0
    removed_faces: list[int] = field(default_factory=list)
    max_displacement: float = 0.0


def _weld(mesh: Mesh, tolerance: float, *, max_removed_fraction: float = 0.0) -> tuple[Mesh, WeldReport]:
    """Build shared indexed topology; never move a source coordinate (clusters keep their first member's position).

    Triangles whose corners merge are collapsed. With ``max_removed_fraction == 0`` any collapse is an error; otherwise collapsed triangles
    are removed (and listed in the report) provided they are at most that fraction of all faces.
    """
    _validated_mesh(mesh)
    if not isinstance(tolerance, (float, int)) or not math.isfinite(tolerance) or tolerance < 0:
        raise GeometryError("Welding tolerance must be a finite non-negative number")
    if mesh.vertices:
        extents = [max(v[i] for v in mesh.vertices) - min(v[i] for v in mesh.vertices) for i in range(3)]
        diagonal = math.sqrt(sum(x * x for x in extents))
        if tolerance and tolerance > max(diagonal * HARD_WELD_FRACTION, 1e-9):
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
    collapsed = [n for n, face in enumerate(faces) if len(set(face)) != 3]
    report = WeldReport(merged_vertices=len(mesh.vertices) - len(vertices))
    if collapsed:
        if not max_removed_fraction:
            raise GeometryError("Welding would collapse one or more triangles; reduce the tolerance")
        if len(collapsed) > max_removed_fraction * len(faces) or len(collapsed) == len(faces):
            raise GeometryError(f"Welding would collapse {len(collapsed)} triangles, more than the policy allows; reduce the tolerance")
        gone = set(collapsed)
        faces = [face for n, face in enumerate(faces) if n not in gone]
        report.removed_faces = collapsed
        used = sorted({i for face in faces for i in face})
        compact = {old: new for new, old in enumerate(used)}
        vertices = [vertices[i] for i in used]
        faces = [[compact[i] for i in face] for face in faces]
        remap = [compact.get(i, -1) for i in remap]
    if tolerance:
        report.max_displacement = max((math.dist(mesh.vertices[i], vertices[r]) for i, r in enumerate(remap) if r >= 0), default=0.0)
    return _validated_mesh(Mesh(vertices, faces, unit=mesh.unit, source_polygons=mesh.source_polygons,
                                warnings=list(mesh.warnings))), report


def weld(mesh: Mesh, tolerance: float = 0.0) -> Mesh:
    """Shared indexed topology. Any collapsed triangle is an error (use the repair policy for removal)."""
    return _weld(mesh, tolerance)[0]


_edges = indexed_edges
_components = face_components


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


def _model_diagonal(mesh: Mesh) -> float:
    return _length([max(v[axis] for v in mesh.vertices) - min(v[axis] for v in mesh.vertices) for axis in range(3)]) if mesh.vertices else 0.0


def boundary_gap_profile(mesh: Mesh, ceiling: float) -> dict[str, Any]:
    """Measure how far apart boundary vertices of an (exactly welded) mesh are from their nearest *unconnected* boundary vertex.

    A defect that tolerance welding can fix shows up as many pairs of boundary vertices that are very close but not joined by any mesh edge.
    Pairs joined by an edge are ordinary short edges, not gaps, and are ignored. Only distances up to ``ceiling`` are searched.
    """
    edges = _edges(mesh)
    joined = set(edges)
    boundary_vertices = sorted({v for (a, b), uses in edges.items() if len(uses) == 1 for v in (a, b)})
    sampled = len(boundary_vertices) > MAX_GAP_VERTICES
    if sampled:
        stride = len(boundary_vertices) / MAX_GAP_VERTICES
        boundary_vertices = [boundary_vertices[int(i * stride)] for i in range(MAX_GAP_VERTICES)]
    profile: dict[str, Any] = {"boundary_vertices": len(boundary_vertices), "sampled": sampled, "ceiling": ceiling,
                               "gaps": [], "paired_vertices": 0}
    if ceiling <= 0 or len(boundary_vertices) < 2:
        return profile
    cells: dict[tuple[int, int, int], list[int]] = defaultdict(list)
    for index in boundary_vertices:
        cells[tuple(math.floor(c / ceiling) for c in mesh.vertices[index])].append(index)
    gaps: list[float] = []
    for index in boundary_vertices:
        vertex = mesh.vertices[index]
        cell = tuple(math.floor(c / ceiling) for c in vertex)
        nearest = math.inf
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    for other in cells.get((cell[0] + dx, cell[1] + dy, cell[2] + dz), ()):
                        if other == index or (min(index, other), max(index, other)) in joined:
                            continue
                        distance = math.dist(vertex, mesh.vertices[other])
                        if distance < nearest:
                            nearest = distance
        if nearest <= ceiling:
            gaps.append(nearest)
    gaps.sort()
    profile["gaps"] = gaps
    profile["paired_vertices"] = len(gaps)
    return profile


def _level_values(diagonal: float, policy: RepairPolicy, gaps: list[float], levels: int) -> list[float]:
    """Ascending, de-duplicated candidate tolerances: the policy's fixed fractions plus levels just above clusters of measured gaps."""
    ceiling = diagonal * min(policy.ceiling_fraction, HARD_WELD_FRACTION)
    values = [diagonal * f for f in policy.ladder if diagonal * f <= ceiling * (1 + 1e-12)]
    if policy.use_measured_gaps and gaps:
        values.append(gaps[0] * 1.0001)                       # the smallest measured gap: the least invasive level that can stitch anything
        top = gaps[0]
        for gap in gaps[1:]:
            if gap > top * 1.25:                              # a visible jump in the gap distribution closes a cluster
                values.append(top * 1.0001)
            top = gap
        values.append(top * 1.0001)
    values = sorted(v for v in values if 0 < v <= ceiling * (1 + 1e-12))
    merged: list[float] = []
    for value in values:
        if not merged or value > merged[-1] * 1.05:
            merged.append(min(value, ceiling))
    if len(merged) > levels:
        keep = {round(i * (len(merged) - 1) / (levels - 1)) for i in range(levels)}
        merged = [v for i, v in enumerate(merged) if i in keep]
    return merged


def _introduces_defects(after: dict[str, Any], before: dict[str, Any]) -> list[str]:
    return [name for name in ("nonmanifold_edges", "degenerate_faces", "inconsistent_winding_edges") if after[name] > before[name]]


def recommend_tolerance(mesh: Mesh, settings: RepairSettings | None = None) -> dict[str, Any]:
    """Recommend the smallest welding tolerance that measurably improves the boundary without introducing defects.

    Tolerance levels come from the repair policy's fixed fractions of the model diagonal and, in balanced/exploratory modes, from the
    *measured* gaps between unconnected boundary vertices. Each level is welded and independently validated; levels that introduce
    non-manifold, degenerate or inconsistently wound edges are rejected, and a larger level is only preferred when it removes enough of
    the remaining boundary (``min_gain_fraction``). A missing surface is never mistaken for a small coordinate discrepancy: when no level
    helps, the tolerance stays 0 and the reason says the openings may be genuine holes.
    """
    settings = settings or DEFAULT_SETTINGS
    policy = settings.policy
    exact = weld(mesh)
    baseline = validate(exact)
    diagonal = _model_diagonal(mesh)
    ceiling = diagonal * min(policy.ceiling_fraction, HARD_WELD_FRACTION)
    recommendation: dict[str, Any] = {
        "tolerance": 0.0, "unit": mesh.unit, "mode": policy.mode, "policy": policy.description,
        "model_diagonal": diagonal, "ceiling": ceiling, "ceiling_fraction": policy.ceiling_fraction, "evaluated_levels": 0,
        "baseline_boundary_edges": baseline["boundary_edges"],
        "expected_boundary_edges": baseline["boundary_edges"],
        "expected_nonmanifold_edges": baseline["nonmanifold_edges"],
        "levels": [], "closure": "none", "needs_reconstruction": False, "removed_faces": 0,
        "reason": "Exact-coordinate reconstruction is the safest starting point.",
        "evidence": [],
    }
    if not baseline["boundary_edges"]:
        recommendation["closure"] = "full"
        recommendation["reason"] = (
            "Exact welding already removes all indexed boundary edges. A larger tolerance "
            "would not close additional holes and could merge intentional features."
        )
        return recommendation
    if diagonal <= 0:
        recommendation["reason"] = "Model dimensions do not permit a meaningful automatic tolerance."
        return recommendation

    profile = boundary_gap_profile(exact, ceiling) if policy.use_measured_gaps else {"gaps": [], "boundary_vertices": 0, "sampled": False, "paired_vertices": 0}
    gaps: list[float] = profile["gaps"]
    if gaps:
        recommendation["gap_profile"] = {
            "boundary_vertices": profile["boundary_vertices"], "paired_vertices": profile["paired_vertices"], "sampled": profile["sampled"],
            "smallest": gaps[0], "median": gaps[len(gaps) // 2], "largest_within_ceiling": gaps[-1]}
        recommendation["evidence"].append(evidence(
            MEASURED, "repair.boundary_gap_profile",
            f"{profile['paired_vertices']} boundary vertices have an unconnected neighbour within {format_tolerance(ceiling, mesh.unit)}",
            smallest_gap=gaps[0], median_gap=gaps[len(gaps) // 2]))
    elif policy.use_measured_gaps:
        recommendation["evidence"].append(evidence(MEASURED, "repair.boundary_gap_profile",
                                                   "No boundary vertex has an unconnected neighbour within the tolerance ceiling"))
    levels = _level_values(diagonal, policy, gaps, policy.max_levels if len(mesh.faces) <= settings.large_mesh_triangles else min(6, policy.max_levels))

    best = baseline
    removed = 0
    for tolerance in levels:
        row: dict[str, Any] = {"tolerance": tolerance, "fraction": tolerance / diagonal}
        recommendation["evaluated_levels"] += 1
        try:
            welded, report = _weld(mesh, tolerance, max_removed_fraction=policy.max_removed_fraction)
            checks = validate(welded)
        except GeometryError as exc:
            row.update(outcome="rejected", reason=str(exc))
            recommendation["levels"].append(row)
            continue
        row.update(boundary_edges=checks["boundary_edges"], nonmanifold_edges=checks["nonmanifold_edges"],
                   degenerate_faces=checks["degenerate_faces"], removed_faces=len(report.removed_faces),
                   max_displacement=report.max_displacement)
        new_defects = _introduces_defects(checks, baseline)
        if new_defects:
            row.update(outcome="rejected", reason="Introduced " + ", ".join(new_defects).replace("_", " "))
        else:
            gain = best["boundary_edges"] - checks["boundary_edges"]
            needed = max(1, math.ceil(policy.min_gain_fraction * best["boundary_edges"])) if best is not baseline else 1
            if gain >= needed:
                row["outcome"] = "accepted"
                best, removed = checks, len(report.removed_faces)
                recommendation.update(tolerance=tolerance, expected_boundary_edges=checks["boundary_edges"],
                                      expected_nonmanifold_edges=checks["nonmanifold_edges"], removed_faces=removed)
                if not checks["boundary_edges"]:
                    row["reason"] = "Closes every indexed boundary edge"
                    recommendation["levels"].append(row)
                    break
            else:
                row.update(outcome="no_gain", reason=("Removes too few boundary edges to justify a larger tolerance"
                                                      if gain > 0 else "No boundary edges removed"))
        recommendation["levels"].append(row)

    remaining = recommendation["expected_boundary_edges"]
    if recommendation["tolerance"]:
        recommendation["closure"] = "full" if not remaining else "partial"
        recommendation["reason"] = (
            "A model-scaled tolerance reduced reconstructed boundary edges without "
            "introducing new non-manifold or degenerate edges in the checks performed. "
            "Review intentional seams before exporting."
        )
        if remaining:
            recommendation["needs_reconstruction"] = True
            recommendation["reason"] += (f" {remaining} boundary edges remain after welding; those need surface reconstruction "
                                         "(hole filling) or may be genuine openings, not coordinate noise.")
        if recommendation["removed_faces"]:
            recommendation["reason"] += f" {recommendation['removed_faces']} collapsed sliver triangle(s) are removed at this level and listed in the candidate."
        recommendation["explanation"] = (
            f"{format_tolerance(recommendation['tolerance'], mesh.unit, diagonal=diagonal)} is the smallest evaluated level "
            f"that gives {remaining} boundary edge(s) (from {baseline['boundary_edges']}); policy: {policy.mode}.")
        recommendation["evidence"].append(evidence(
            ESTIMATED, "repair.recommend_tolerance",
            "Tolerance chosen from the evaluated levels; the topology outcome of that level was measured by weld + validate",
            tolerance=recommendation["tolerance"], levels_evaluated=recommendation["evaluated_levels"]))
    else:
        recommendation["needs_reconstruction"] = True
        recommendation["reason"] = (
            "No safe automatic welding level improved the boundary topology. "
            "The model may contain genuine holes or intentional openings; inspect repair patches."
        )
        recommendation["explanation"] = (
            f"None of the {recommendation['evaluated_levels']} evaluated level(s) up to "
            f"{format_tolerance(ceiling, mesh.unit, diagonal=diagonal)} improved the {baseline['boundary_edges']} boundary edge(s) "
            "without side effects; they look like missing surface rather than coordinate noise.")
    return recommendation


def diagnose(mesh: Mesh, tolerance: float = 0.0, settings: RepairSettings | None = None) -> dict[str, Any]:
    settings = settings or DEFAULT_SETTINGS
    original = validate(mesh)
    reconstructed = weld(mesh, tolerance)
    updated = validate(reconstructed)
    loops, chains = _boundary_loops(_edges(reconstructed))
    sample = []
    for loop in loops[:settings.max_diagnostic_loops]:
        length = sum(_length(_subtract(reconstructed.vertices[a], reconstructed.vertices[b]))
                     for a, b in zip(loop, loop[1:] + loop[:1]))
        sample.append({"edges": len(loop), "perimeter": length})
    return {
        "source": original, "reconstructed": updated,
        "tolerance": tolerance, "unit": mesh.unit,
        "coincident_records": len(mesh.vertices) - len({tuple(v) for v in mesh.vertices}),
        "boundary_loops": sample, "loops_truncated": len(loops) > len(sample),
        "open_chains": chains, "mode": settings.mode,
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


def _planar_points(mesh: Mesh, loop: list[int], max_edges: int = MAX_PATCH_EDGES) -> tuple[list[tuple[float, float]], float] | None:
    if not 3 <= len(loop) <= max_edges:
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


def _fill_planar(mesh: Mesh, *, fan: bool, max_edges: int = MAX_PATCH_EDGES,
                 max_loops: int = MAX_DIAGNOSTIC_LOOPS) -> tuple[Mesh, list[int]] | None:
    loops, open_chains = _boundary_loops(_edges(mesh))
    if open_chains or not loops or len(loops) > max_loops:
        return None
    vertices = [list(v) for v in mesh.vertices]
    faces = [list(f) for f in mesh.faces]
    appended = []
    for loop in loops:
        planar = _planar_points(mesh, loop, max_edges)
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


_OBJECTIVES = ("watertight", "risk", "nonmanifold_edges", "boundary_edges", "degenerate_faces", "inconsistent_winding_edges",
               "deviation", "topology_changes")


def _objective_vector(candidate: Candidate) -> dict[str, float]:
    v = candidate.validation
    return {"watertight": 0 if v["watertight"] else 1, "risk": 0 if candidate.risk == "none_identified" else 1,
            "nonmanifold_edges": v["nonmanifold_edges"], "boundary_edges": v["boundary_edges"],
            "degenerate_faces": v["degenerate_faces"], "inconsistent_winding_edges": v["inconsistent_winding_edges"],
            "deviation": float(candidate.deviation or 0.0),
            "topology_changes": len(candidate.added_faces) + len(candidate.removed_face_indices)}


def rank_candidates(items: list[Candidate]) -> list[Candidate]:
    """Deterministic lexicographic ranking on measured objectives, with a Pareto flag.

    Order: closed topology first; then no exploratory risk; then fewest non-manifold edges, boundary edges, degenerate faces and
    inconsistently wound edges; then smallest measured vertex displacement; then fewest added/removed faces; strategy name breaks ties.
    No score is invented: every number in ``ranking.objectives`` is read from the candidate's own validation or weld report.
    """
    vectors = {id(c): _objective_vector(c) for c in items}

    def dominated(candidate: Candidate) -> bool:
        mine = vectors[id(candidate)]
        return any(all(other[k] <= mine[k] for k in _OBJECTIVES) and any(other[k] < mine[k] for k in _OBJECTIVES)
                   for key, other in vectors.items() if key != id(candidate))

    ordered = sorted(items, key=lambda c: (*[vectors[id(c)][k] for k in _OBJECTIVES], c.strategy))
    for rank, candidate in enumerate(ordered, 1):
        candidate.ranking = {"rank": rank, "objectives": vectors[id(candidate)], "criteria_order": list(_OBJECTIVES),
                             "pareto_optimal": not dominated(candidate), "method": "deterministic lexicographic ranking on measured values"}
    return ordered


def candidates(mesh: Mesh, tolerance: float = 0.0, limit: int = 5, settings: RepairSettings | None = None) -> list[Candidate]:
    settings = settings or DEFAULT_SETTINGS
    policy = settings.policy
    if not isinstance(limit, int) or not 1 <= limit <= 5:
        raise GeometryError("Choose between one and five repair variants")
    original = validate(mesh)
    base = weld(mesh)
    base_validation = validate(base)
    built: list[Candidate] = []

    def append(strategy: str, label: str, result: Mesh, added: list[int], explanation: str, *, used_tolerance: float = 0.0,
               removed: list[int] | None = None, deviation: float | None = None, notes: list[str] | None = None) -> None:
        if len(built) >= 5 or not settings.allows(strategy):
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
        beyond_conservative = used_tolerance > _model_diagonal(mesh) * CONSERVATIVE_CEILING * (1 + 1e-9)
        risky = bool(removed) or beyond_conservative or policy.mode == "exploratory" and used_tolerance > 0
        built.append(Candidate(strategy, label, result, added, explanation, len(mesh.faces), checks,
                               tolerance=used_tolerance, removed_face_indices=list(removed or []), deviation=deviation,
                               risk="exploratory" if risky else "none_identified", mode=policy.mode,
                               notes=list(notes or []), baseline=base_validation))

    patch_base, patch_tolerance = base, 0.0
    if len(base.vertices) != len(mesh.vertices) or base_validation != original:
        append("topology", "Exact topology reconstruction", base, [],
               "Reindex identical coordinates without adding or moving surface faces.", deviation=0.0)
    if tolerance > 0:
        try:
            approximate, report = _weld(mesh, tolerance, max_removed_fraction=policy.max_removed_fraction)
        except GeometryError:
            approximate = report = None
        if approximate is not None and len(approximate.vertices) < len(base.vertices):
            extra = ([f"{len(report.removed_faces)} collapsed sliver triangle(s) removed"] if report.removed_faces else [])
            append("tolerant_weld", "Tolerance-based stitching", approximate, [],
                   f"Stitch coordinates within {format_tolerance(tolerance, mesh.unit)}; this may join intentional seams.",
                   used_tolerance=tolerance, removed=report.removed_faces, deviation=report.max_displacement, notes=extra)
            accepted = built[-1] if built and built[-1].strategy == "tolerant_weld" else None
            if accepted is not None and not _introduces_defects(accepted.validation, base_validation):
                patch_base, patch_tolerance = approximate, tolerance   # patches are built on the stitched surface
    isolated = _isolate_shells(base)
    if isolated:
        append("isolate_shells", "Separate touching manifold shells", isolated, [],
               "Split coincident topology by manifold face components; geometric shell overlaps remain unverified.", deviation=0.0)
    for fan, strategy, label in ((False, "hole_fill", "Local planar hole triangulation"),
                                 (True, "planar_cap", "Planar centroid caps")):
        if not settings.allows(strategy):
            continue
        repaired = _fill_planar(patch_base, fan=fan, max_edges=settings.max_patch_edges, max_loops=settings.max_diagnostic_loops)
        if repaired:
            result, added = repaired
            note = [f"Patches were built on the surface stitched at {format_tolerance(patch_tolerance, mesh.unit)}"] if patch_tolerance else []
            append(strategy, label, result, added,
                   "Create red faces on verified planar boundary loops. Intentional openings require user review.",
                   used_tolerance=patch_tolerance, deviation=0.0, notes=note)
    return rank_candidates(built)[:limit]


def capability_info(settings: RepairSettings | None = None) -> dict[str, Any]:
    settings = settings or DEFAULT_SETTINGS
    conditional = {"tolerant_weld": "Requires explicit positive tolerance",
                   "isolate_shells": "Requires separable manifold face components",
                   "hole_fill": f"Requires simple planar boundary loops up to {settings.max_patch_edges} edges",
                   "planar_cap": f"Requires convex planar boundary loops up to {settings.max_patch_edges} edges"}
    return {
        "available": [name for name in STRATEGIES if settings.allows(name)],
        "disabled": {name: "Disabled by repair.enabled_strategies in cadcon_config.yaml" for name in STRATEGIES if not settings.allows(name)},
        "conditional": {name: text for name, text in conditional.items() if settings.allows(name)},
        "unavailable": {"curvature_reconstruction": "No verified curvature-fairing backend configured",
                        "volumetric_reconstruction": "No verified volumetric-remeshing backend configured"},
        "mode": settings.mode, "policy": settings.policy.__dict__ | {"ladder": list(settings.policy.ladder)},
        "limits": {"max_candidates": settings.max_candidates, "max_patch_edges": settings.max_patch_edges,
                   "max_diagnostic_loops": settings.max_diagnostic_loops},
        "agent_runtime": "unconfigured; deterministic repair remains available",
    }
