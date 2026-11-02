"""CADCon's thin composition root, using SLAI's shared local application server."""
from __future__ import annotations

import argparse
import copy
import errno
import hashlib
import importlib
import os
import secrets
import struct
import sys
import threading
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Any

APP_DIR = Path(__file__).resolve().parent
VERSION = "1.0.0-dev"


def _module(name: str):
    """Import a CADCon module in either layout (``applications.CADCon.x`` or flat ``x``)."""
    return importlib.import_module(f"{__package__}.{name}" if __package__ else name)


def _activate_slai(explicit: str | None) -> Path | None:
    """Discover SLAI without importing or vendoring its existing shared tools."""
    candidates = [Path(explicit)] if explicit else []
    if os.environ.get("SLAI_ROOT"):
        candidates.append(Path(os.environ["SLAI_ROOT"]))
    candidates.extend(APP_DIR.parents)
    for root in candidates:
        if (root / "applications" / "tools" / "server" / "http.py").is_file():
            root = root.resolve()
            if str(root) not in sys.path:
                sys.path.append(str(root))
            return root
    return None


class CADCon:
    def __init__(self, data_dir: Path, dwg_executable: str | None = None, slai_root: Path | None = None, config: Any = None) -> None:
        self.lock = threading.RLock()
        self.analysis_cache: OrderedDict[tuple, dict[str, Any]] = OrderedDict()
        self.cache_stats = {"hits": 0, "misses": 0}
        self.apply_config(config if config is not None else _module("utils.config_loader").get_config())
        self.data_dir = data_dir
        self.upload_dir = data_dir / "uploads"
        self.output_dir = data_dir / "outputs"
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.dwg_executable = dwg_executable or self.config.get("conversion.dwg_executable")
        self.slai_root = slai_root
        self.files: dict[str, Path] = {}
        self.original_names: dict[str, str] = {}
        self.outputs: dict[str, Path] = {}
        self.reports: dict[str, dict[str, Any]] = {}
        self.report_sources: dict[str, str] = {}
        self.report_previews: dict[str, bytes] = {}
        self.report_repair_previews: dict[str, bytes] = {}
        self.repair_candidates: dict[str, tuple[str, Any, str, str]] = {}
        self.repair_diagnoses: dict[str, dict[str, Any]] = {}
        self.output_names: dict[str, str] = {}

    def apply_config(self, config: Any) -> None:
        """Make ``config`` the live configuration: engine limits, repair settings and the agent runtime all follow it."""
        self.config = config
        engine = _module("engine")
        engine.configure(max_source_bytes=config.get("files.max_source_bytes"), max_triangles=config.get("geometry.max_triangles"),
                         preview_triangle_limit=config.get("visualization.preview_triangle_limit"))
        self.settings = _module("repair").RepairSettings.from_config(config)
        self.runtime = _module("slai.bridge.agents").get_runtime()
        self.runtime.set_config(config)
        self.runtime.reset_cooldown()
        with self.lock:
            self.analysis_cache.clear()

    def cached(self, kind: str, path: Path, unit: str, build: Any) -> dict[str, Any]:
        """Bounded LRU of inspection/analysis results keyed by file identity and unit (``performance.analysis_cache_entries``)."""
        size = int(self.config.get("performance.analysis_cache_entries"))
        if size <= 0:
            return build()
        stat = path.stat()
        key = (kind, str(path), stat.st_size, stat.st_mtime_ns, unit)
        with self.lock:
            if key in self.analysis_cache:
                self.analysis_cache.move_to_end(key)
                self.cache_stats["hits"] += 1
                return copy.deepcopy(self.analysis_cache[key])
        value = build()
        with self.lock:
            self.cache_stats["misses"] += 1
            self.analysis_cache[key] = copy.deepcopy(value)
            while len(self.analysis_cache) > size:
                self.analysis_cache.popitem(last=False)
        return value

    def close(self) -> None:
        """Release the SLAI agents CADCon loaded (through the SLAI AgentFactory)."""
        _module("slai.bridge.agents").shutdown_runtime()

    def get_file(self, identity: str) -> Path:
        from applications.tools.errors import NotFound
        with self.lock:
            path = self.files.get(identity)
        if path is None or not path.is_file():
            raise NotFound("The selected file is no longer available")
        return path


def build_router():
    from applications.tools.errors import BadRequest, LimitExceeded, NotFound
    from applications.tools.server import Router, Response, file_response
    from applications.tools.storage import atomic_write
    from src.utils.sldc import SLDCError
    from src.utils.spatial_formats.common.errors import SpatialFormatError
    module = importlib.import_module(f"{__package__}.engine" if __package__ else "engine")
    repairs = importlib.import_module(f"{__package__}.repair" if __package__ else "repair")
    agent_bridge = _module("slai.bridge.agents")
    adapters = _module("slai.bridge.adapters")
    errors = _module("utils.cadcon_errors")
    helpers_module = _module("utils.cadcon_helpers")
    config_loader = _module("utils.config_loader")
    router = Router()

    def unit_of(app, data):
        unit = data.get("unit", app.config.get("geometry.default_unit"))
        if not isinstance(unit, str):
            raise BadRequest("Unit must be a text value such as 'mm'")
        return unit

    def run(action, *args, **kwargs):
        try:
            return action(*args, **kwargs)
        except Exception as exc:
            mapped = errors.classify_exception(exc)  # expected input/capability conditions only; real defects propagate unchanged
            if mapped is None or mapped is exc:
                raise
            raise mapped from exc

    def read_png_preview(req, app):
        from src.utils import sldc
        limit_bytes = int(app.config.get("reporting.max_preview_bytes"))
        if not req.length or not 0 < req.length <= limit_bytes:
            raise LimitExceeded(f"PNG preview must be between 1 byte and {helpers_module.format_bytes(limit_bytes)}")
        image = req.stream.read(req.length) if req.stream else b""
        req._read = True
        if (len(image) != req.length or len(image) < 45 or
                image[:16] != b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" or
                image[-12:] != b"\x00\x00\x00\x00IEND\xaeB`\x82"):
            raise BadRequest("Report preview must be a valid PNG image")
        width, height = struct.unpack(">II", image[16:24])
        if not 0 < width <= 4096 or not 0 < height <= 4096:
            raise BadRequest("Preview dimensions exceed the supported 4096 pixel limit")
        try:
            sldc.make_asset(image)
        except sldc.SLDCError as exc:
            raise BadRequest(f"The model preview is invalid: {exc}") from exc
        return image, width, height

    def repair_tolerance(app, mesh, requested):
        recommendation = run(repairs.recommend_tolerance, mesh, app.settings)
        if requested is None or requested == "auto" or requested == "":
            return recommendation["tolerance"], "automatic", recommendation
        if type(requested) not in (float, int):
            raise BadRequest("Welding tolerance must be a number or 'auto'")
        value = float(requested)
        if not 0 <= value < float("inf"):
            raise BadRequest("Welding tolerance must be finite and non-negative")
        return value, "manual", recommendation

    def repair_diagnosis(app, mesh, requested):
        tolerance, mode, recommendation = repair_tolerance(app, mesh, requested)
        try:
            result = run(repairs.diagnose, mesh, tolerance, app.settings)
        except BadRequest as exc:
            raise BadRequest(f"{exc}. Try Automatic tolerance; the suggested value is "
                             f"{recommendation['tolerance']:.6g} {mesh.unit or 'source units'}") from exc
        result["tolerance_mode"] = mode
        result["recommendation"] = recommendation
        return result

    @router.route("GET", "/api/health")
    def health(app, req):
        return {"application": app.config.get("application.name"), "version": VERSION, "status": "ready"}

    @router.route("GET", "/api/capabilities")
    def capabilities(app, req):
        return module.capabilities(dwg_executable=app.dwg_executable)

    def agent_report(app, probe=False):
        return [app.runtime.health(key, probe=probe) for key in config_loader.INTEGRATED_AGENTS]

    def config_report(app):
        development = app.config.get("application.mode") == "development"
        return {"mode": app.config.get("application.mode"), "diagnostics": app.config.diagnostics(show_path=development),
                "values": app.config.to_dict(), "workflows": {w: list(app.config.route(w)) for w in config_loader.WORKFLOWS}}

    @router.route("GET", "/api/system")
    def system(app, req):
        engine_limits = module.limits()
        return {"version": VERSION, "python": sys.version.split()[0], "mode": app.config.get("application.mode"),
                "slai_root": str(app.slai_root) if app.slai_root and app.config.get("application.mode") == "development" else bool(app.slai_root),
                "dwg_backend": bool(app.dwg_executable),
                "uploaded_files": len(app.files), "outputs": len(app.outputs), "limits": engine_limits,
                "configuration": app.config.diagnostics(), "repair": repairs.capability_info(app.settings),
                "cache": {**app.cache_stats, "entries": len(app.analysis_cache), "capacity": app.config.get("performance.analysis_cache_entries")},
                "agents": {"enabled": app.config.get("agents.enabled"), "integrations": agent_report(app),
                           "workflows": {w: list(app.config.route(w)) for w in config_loader.WORKFLOWS},
                           "metrics": app.runtime.metrics(),
                           "not_integrated": "Other SLAI agents have no CADCon adapter yet and cannot be enabled"}}

    @router.route("GET", "/api/agents")
    def agents_status(app, req):
        probe = req.str_query("probe", "0") in ("1", "true")
        return {"enabled": app.config.get("agents.enabled"), "agents": agent_report(app, probe=probe),
                "workflows": {w: list(app.config.route(w)) for w in config_loader.WORKFLOWS}, "metrics": app.runtime.metrics()}

    @router.route("GET", "/api/config")
    def config_view(app, req):
        return config_report(app)

    @router.route("POST", "/api/config/reload")
    def config_reload(app, req):
        try:
            fresh = config_loader.reload_config()
        except errors.ConfigurationError:
            raise  # the previous configuration stays active; the message names the offending key
        app.apply_config(fresh)
        return config_report(app)

    @router.route("POST", "/api/files/import")
    def upload(app, req):
        if not req.length or not 0 < req.length <= module.MAX_SOURCE_BYTES:
            raise LimitExceeded(f"File is empty or exceeds the {helpers_module.format_bytes(module.MAX_SOURCE_BYTES)} limit")
        with app.lock:
            if len(app.files) >= int(app.config.get("files.max_active_uploads")):
                raise LimitExceeded("Too many imported models are active; remove unused models first")
        name = req.str_query("name", max_len=180)
        suffix = Path(name).suffix.lower()
        if suffix not in module.THREE_D | module.TWO_D | module.OPTIONAL_FORMATS:
            raise BadRequest("Unrecognized CAD file extension")
        # Keep a bounded exact read; no client paths and no shell execution.
        payload = req.stream.read(req.length) if req.stream else b""
        req._read = True
        if len(payload) != req.length:
            raise BadRequest("File upload ended unexpectedly")
        identity = uuid.uuid4().hex
        destination = app.upload_dir / f"{identity}{suffix}"
        atomic_write(destination, payload, exclusive=True)
        with app.lock:
            app.files[identity] = destination
            app.original_names[identity] = Path(name).name
        return {"id": identity, "name": Path(name).name, "size_bytes": len(payload), "format": suffix.lstrip(".")}

    @router.route("GET", "/api/files")
    def files(app, req):
        with app.lock:
            return [{"id": key, "name": app.original_names.get(key, path.name), "size_bytes": path.stat().st_size}
                    for key, path in app.files.items() if path.is_file()]

    @router.route("DELETE", "/api/files/{id}")
    def delete_file(app, req):
        with app.lock:
            path = app.files.pop(req.params["id"], None)
            app.original_names.pop(req.params["id"], None)
            app.repair_diagnoses.pop(req.params["id"], None)
            for identity, entry in list(app.repair_candidates.items()):
                if entry[0] == req.params["id"]:
                    del app.repair_candidates[identity]
        if path is None:
            raise NotFound("Unknown file")
        path.unlink(missing_ok=True)
        return {"removed": True}

    @router.route("POST", "/api/inspect")
    def inspect(app, req):
        data = req.json()
        path = app.get_file(str(data.get("id", "")))
        unit = unit_of(app, data)
        result = run(app.cached, "inspect", path, unit, lambda: module.inspect(path, unit=unit, dwg_executable=app.dwg_executable))
        result["source"]["name"] = app.original_names.get(str(data.get("id")), path.name)
        return result

    @router.route("POST", "/api/convert")
    def convert(app, req):
        data = req.json()
        source = app.get_file(str(data.get("id", "")))
        target = data.get("format")
        if not isinstance(target, str) or not target.isalpha() or len(target) > 5:
            raise BadRequest("Choose a supported output format")
        ext = "." + target.lower()
        if ext not in module.NATIVE_3D_OUTPUT | module.NATIVE_2D_OUTPUT:
            raise BadRequest("No verified converter for this format")
        identity = uuid.uuid4().hex
        path = app.output_dir / (identity + ext)
        result = run(module.convert, source, path, policy=data.get("policy", app.config.get("conversion.default_policy")),
                     unit=unit_of(app, data), dwg_executable=app.dwg_executable)
        source_name = app.original_names.get(str(data.get("id")), source.name)
        result["source"] = source_name
        result["target"] = Path(source_name).stem + ext
        with app.lock:
            app.outputs[identity] = path
            app.output_names[identity] = result["target"]
        return {"id": identity, **result}

    @router.route("GET", "/api/repair/capabilities")
    def repair_capabilities(app, req):
        return repairs.capability_info(app.settings)

    @router.route("POST", "/api/repair/analyze")
    def repair_analyze(app, req):
        data = req.json()
        source_id = str(data.get("id", ""))
        path = app.get_file(source_id)
        mesh = run(module.load_mesh, path, unit=unit_of(app, data))
        diagnosis = repair_diagnosis(app, mesh, data.get("tolerance", "auto"))
        with app.lock:
            app.repair_diagnoses[source_id] = diagnosis
        return {"diagnosis": diagnosis, "capabilities": repairs.capability_info(app.settings)}

    @router.route("POST", "/api/repair/suggest")
    def repair_suggest(app, req):
        data = req.json()
        source_id = str(data.get("id", ""))
        path = app.get_file(source_id)
        limit = data.get("max_candidates")
        agent_enabled = data.get("use_agents", True)
        if limit is not None and (type(limit) is not int or limit < 1):
            raise BadRequest("Candidate count must be a positive integer")
        if not isinstance(agent_enabled, bool):
            raise BadRequest("use_agents must be a Boolean")
        mesh = run(module.load_mesh, path, unit=unit_of(app, data))
        diagnosis = repair_diagnosis(app, mesh, data.get("tolerance", "auto"))
        tol = diagnosis["tolerance"]
        limit = app.settings.candidate_limit(len(mesh.faces), limit)  # configured cap, tighter for very large meshes
        generated = run(repairs.candidates, mesh, tol, limit, app.settings)
        original_name = app.original_names.get(source_id, path.name)
        source_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        outputs = []
        with app.lock:
            app.repair_diagnoses[source_id] = diagnosis
            for existing, entry in list(app.repair_candidates.items()):
                if entry[0] == source_id:
                    del app.repair_candidates[existing]
            if len(app.repair_candidates) + len(generated) > 25:
                raise LimitExceeded("Too many repair candidates are active; remove unused imported models")
            for candidate in generated:
                identity = uuid.uuid4().hex
                app.repair_candidates[identity] = (source_id, candidate, original_name, source_hash)
                outputs.append({"id": identity, **candidate.details()})
            repair_result = {"diagnosis": diagnosis, "candidates": outputs}
        if agent_enabled:
            # Postconditions first (fast, bounded), then the optional language explanation: a slow Reasoning Agent never delays them.
            for entry in outputs:
                entry["agent_checks"] = [r.to_dict() for r in adapters.candidate_postconditions(
                    app.runtime, diagnosis.get("source", {}), entry.get("validation", {}), label=entry.get("strategy", "repair"))]
        repair_result["agents"] = agent_bridge.mesh_repair_advice(diagnosis, outputs, enabled=agent_enabled, runtime=app.runtime)
        with app.lock:
            for report_id, report_source in app.report_sources.items():
                if report_source == source_id and report_id in app.reports:
                    app.report_repair_previews.pop(report_id, None)
                    app.reports[report_id]["repair"] = repair_result
                    app.reports[report_id].pop("ai_analysis", None)
                    if repair_result["agents"].get("status") == "completed":
                        agent = repair_result["agents"]
                        app.reports[report_id]["ai_analysis"] = {
                            "agent": agent["agent"], "evidence": agent["evidence"],
                            "summary": agent["interpretation"], "limitations": agent["limitations"]}
        return repair_result

    @router.route("GET", "/api/repair/candidates/{id}")
    def repair_candidate(app, req):
        with app.lock:
            entry = app.repair_candidates.get(req.params["id"])
        if entry is None:
            raise NotFound("Repair candidate not found")
        return {"id": req.params["id"], "source_name": entry[2], **entry[1].details()}

    @router.route("GET", "/api/repair/candidates/{id}/preview")
    def repair_preview(app, req):
        with app.lock:
            entry = app.repair_candidates.get(req.params["id"])
        if entry is None:
            raise NotFound("Repair candidate not found")
        candidate = entry[1]
        mesh = candidate.mesh
        limit = module.PREVIEW_TRIANGLE_LIMIT
        # Reserve preview capacity for actual new faces; never drop small red
        # repair patches from a large source preview.
        repair_indices = candidate.added_faces
        if len(repair_indices) > limit:
            step = len(repair_indices) / limit
            chosen = [repair_indices[int(i * step)] for i in range(limit)]
        else:
            base_budget = max(0, limit - len(repair_indices))
            repair_set = set(repair_indices)
            base_indices = [i for i in range(len(mesh.faces)) if i not in repair_set]
            if len(base_indices) > base_budget and base_budget:
                step = len(base_indices) / base_budget
                base_indices = [base_indices[int(i * step)] for i in range(base_budget)]
            chosen = base_indices[:base_budget] + list(repair_indices)
        needed = sorted({idx for i in chosen for idx in mesh.faces[i]})
        remap = {idx: i for i, idx in enumerate(needed)}
        added = set(repair_indices)
        return {"kind": "mesh", "vertices": [mesh.vertices[i] for i in needed],
                "faces": [[remap[i] for i in mesh.faces[index]] for index in chosen],
                "new_faces": [i for i, index in enumerate(chosen) if index in added],
                "partial": len(mesh.faces) > len(chosen),
                "total_triangles": len(mesh.faces), "preview_limit": limit}

    @router.route("POST", "/api/repair/candidates/{id}/export")
    def repair_export(app, req):
        data = req.json()
        with app.lock:
            entry = app.repair_candidates.get(req.params["id"])
        if entry is None:
            raise NotFound("Repair candidate not found")
        source_id, candidate, original_name, source_hash = entry
        app.get_file(source_id)
        target = data.get("format", "sgeo")
        if target not in ("sgeo", "obj"):
            raise BadRequest("Verified repair export is currently available for SGEO and OBJ")
        identity = uuid.uuid4().hex
        destination = app.output_dir / (identity + "." + target)
        if target == "sgeo":
            def create_native():
                SGEODocument, _ = module._spatial()
                document = SGEODocument.new(Path(original_name).stem + " repaired", unit=candidate.mesh.unit or "mm")
                document.add_mesh(candidate.mesh.vertices, candidate.mesh.faces,
                                  name="repaired_" + candidate.strategy,
                                  metadata={"cadcon:repair": {"strategy": candidate.strategy,
                                           "source_sha256": source_hash,
                                           "source_face_count": candidate.source_face_count,
                                           "added_face_indices": candidate.added_faces,
                                           "validation": candidate.validation}})
                module._safe_write(destination, document.dumps())
            run(create_native)
        else:
            run(module._safe_write, destination, module._obj_bytes(candidate.mesh))
        try:
            reopened = run(module.load_mesh, destination, unit=candidate.mesh.unit or "mm")
            if len(reopened.faces) != len(candidate.mesh.faces):
                raise module.GeometryError("Repair export round-trip changed the triangle count")
            round_trip = run(repairs.validate, reopened)
            for metric in ("boundary_edges", "nonmanifold_edges", "degenerate_faces",
                           "inconsistent_winding_edges", "topologically_closed", "consistent_winding"):
                if round_trip[metric] != candidate.validation[metric]:
                    raise module.GeometryError(f"Repair export changed verified topology: {metric}")
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        safe_stem = module.report_filename({"source": {"name": original_name}}, "json")
        model_name = safe_stem.removeprefix("cadcon-analysis-").removesuffix(".json")
        filename = f"{model_name}-repaired-{candidate.strategy}.{target}"
        with app.lock:
            app.outputs[identity] = destination
            app.output_names[identity] = filename
        return {"id": identity, "filename": filename, "source_sha256": source_hash,
                "validation": candidate.validation, "export_verified": True}

    @router.route("POST", "/api/analyze")
    def analyze(app, req):
        data = req.json()
        path = app.get_file(str(data.get("id", "")))
        unit = unit_of(app, data)
        report = run(app.cached, "analysis", path, unit, lambda: module.analysis(path, unit=unit))
        report["source"]["name"] = app.original_names.get(str(data.get("id")), path.name)
        identity = uuid.uuid4().hex
        with app.lock:
            app.reports[identity] = report
            app.report_sources[identity] = str(data.get("id"))
        return {"id": identity, "report": report,
                "filenames": {fmt: module.report_filename(report, fmt, prefix=app.config.get("reporting.filename_prefix")) for fmt in ("json", "sldc")}}

    @router.route("POST", "/api/reports/{id}/interpret")
    def report_interpret(app, req):
        identity = req.params["id"]
        with app.lock:
            report = app.reports.get(identity)
        if report is None or report.get("report_kind") != "cadcon.analysis":
            raise NotFound("Unknown geometry analysis report")
        outcome = adapters.analysis_interpretation(app.runtime, report.get("findings", []), report.get("geometry_summary", {}))
        record = outcome.to_dict()
        if outcome.ok:
            with app.lock:
                app.reports[identity]["ai_analysis"] = {"agent": "src.agents.reasoning_agent.ReasoningAgent", "evidence": outcome.result["grounded_in"],
                                                        "evidence_records": outcome.evidence, "summary": outcome.result["interpretation"],
                                                        "limitations": "; ".join(outcome.limitations)}
        return record

    @router.route("POST", "/api/reports/{id}/preview")
    def report_preview(app, req):
        identity = req.params["id"]
        source_id = req.str_query("source_id")
        with app.lock:
            expected_source_id = app.report_sources.get(identity)
        if not expected_source_id:
            raise NotFound("Unknown geometry analysis report")
        if source_id != expected_source_id:
            raise BadRequest("The screenshot is not associated with this report's source model")
        image, width, height = read_png_preview(req, app)
        with app.lock:
            if app.report_sources.get(identity) != source_id:
                raise BadRequest("The report was replaced during preview generation")
            app.report_previews[identity] = image
            triangle_count = app.reports[identity].get("geometry_summary", {}).get("triangles")
            app.reports[identity]["preview"] = {"format": "png", "width": width, "height": height,
                                                 "sha256": hashlib.sha256(image).hexdigest(),
                                                 "renderer": "CADCon geometry viewer",
                                                 "preview_triangle_limit": module.PREVIEW_TRIANGLE_LIMIT,
                                                 "partial": (triangle_count > module.PREVIEW_TRIANGLE_LIMIT
                                                             if isinstance(triangle_count, int) else False)}
        return {"attached": True, "width": width, "height": height}

    @router.route("POST", "/api/reports/{id}/repair-preview")
    def repair_report_preview(app, req):
        report_id = req.params["id"]
        candidate_id = req.str_query("candidate_id")
        with app.lock:
            report_source = app.report_sources.get(report_id)
            entry = app.repair_candidates.get(candidate_id)
        if not report_source or entry is None or entry[0] != report_source:
            raise BadRequest("The selected repair candidate does not belong to this analysis report")
        image, width, height = read_png_preview(req, app)
        with app.lock:
            if app.report_sources.get(report_id) != entry[0]:
                raise BadRequest("The report changed during repair preview generation")
            app.report_repair_previews[report_id] = image
            app.reports[report_id].setdefault("repair", {})["selected_preview"] = {
                "candidate_id": candidate_id, "format": "png", "width": width, "height": height,
                "sha256": hashlib.sha256(image).hexdigest()}
        return {"attached": True, "width": width, "height": height}

    def compare_registration(app, first, second, unit, result):
        """Rigid registration of the right model's sampled surface onto the left's with the SLAI Spatial Agent (an estimate, labelled so)."""
        if result.get("status") in ("identical", "incomparable") or result.get("left", {}).get("geometry_summary", {}).get("representation") != "triangle_mesh":
            return {"status": "not_applicable", "reason": "Registration applies to two different triangle meshes", "evidence": [
                helpers_module.evidence(helpers_module.UNAVAILABLE, "cadcon.compare", "Registration was not run", reason="not applicable")]}
        left, right = run(module.load_mesh, first, unit=unit), run(module.load_mesh, second, unit=unit)
        factor = (result.get("unit_normalised") or {}).get("factor", 1.0)
        count = 300
        source = [[x * factor for x in p] for p in module.sample_points(right, count)]
        target = module.sample_points(left, count)
        size = min(len(source), len(target))
        outcome = adapters.register_point_sets(app.runtime, source[:size], target[:size])
        record = outcome.to_dict()
        record["samples"] = size
        record["unit"] = result["left"]["geometry_summary"].get("unit")
        record["direction"] = "right model onto left model"
        return record

    @router.route("POST", "/api/compare")
    def compare(app, req):
        data = req.json()
        a = app.get_file(str(data.get("left", "")))
        b = app.get_file(str(data.get("right", "")))
        try:
            tol = float(data.get("tolerance", app.config.get("comparison.default_tolerance")))
        except (ValueError, TypeError) as exc:
            raise BadRequest("Tolerance must be a number") from exc
        register = data.get("register", False)
        if not isinstance(register, bool):
            raise BadRequest("register must be a Boolean")
        unit = unit_of(app, data)
        result = run(module.compare, a, b, tolerance=tol, unit=unit)
        if register:
            result["registration"] = compare_registration(app, a, b, unit, result)
        result["left"]["source"]["name"] = app.original_names.get(str(data.get("left")), a.name)
        result["right"]["source"]["name"] = app.original_names.get(str(data.get("right")), b.name)
        identity = uuid.uuid4().hex
        with app.lock:
            app.reports[identity] = {"report_kind": "cadcon.comparison", "report_version": "1.0.0", **result}
        return {"report_id": identity, **result}

    @router.route("POST", "/api/spatial/nearest")
    def spatial_nearest(app, req):
        data = req.json()
        source = app.get_file(str(data.get("id", "")))
        return run(module.spatial_nearest, source, data.get("point_m", [0, 0, 0]), unit=unit_of(app, data))

    @router.route("GET", "/api/files/{id}/geometry")
    def geometry(app, req):
        path = app.get_file(req.params["id"])
        if path.suffix.lower() in module.TWO_D:
            kind, doc = run(module._scene, path, dwg_executable=app.dwg_executable)
            svg, _ = doc.to_svg()
            return {"kind": "svg", "markup": svg}
        mesh = run(module.load_mesh, path)
        limit = module.PREVIEW_TRIANGLE_LIMIT
        faces = mesh.faces[:limit]
        needed = sorted({index for face in faces for index in face})
        remap = {index: i for i, index in enumerate(needed)}
        return {"kind": "mesh", "vertices": [mesh.vertices[i] for i in needed],
                "faces": [[remap[i] for i in f] for f in faces],
                "partial": len(mesh.faces) > limit, "total_triangles": len(mesh.faces),
                "preview_limit": limit}

    @router.route("GET", "/api/outputs/{id}")
    def output(app, req):
        with app.lock:
            path = app.outputs.get(req.params["id"])
        if not path or not path.is_file():
            raise NotFound("Output not available")
        return file_response(path.read_bytes(), app.output_names.get(req.params["id"], "cadcon-converted" + path.suffix), "application/octet-stream")

    @router.route("GET", "/api/reports/{id}")
    def report(app, req):
        with app.lock:
            data = app.reports.get(req.params["id"])
            preview = app.report_previews.get(req.params["id"])
            repair_preview = app.report_repair_previews.get(req.params["id"])
        if data is None:
            raise NotFound("Report not available")
        fmt = req.str_query("format", "json")
        if fmt == "sldc" and data.get("report_kind") == "cadcon.analysis" and preview is None:
            raise BadRequest("Generate the model preview before exporting the native SLDC report")
        prefix = app.config.get("reporting.filename_prefix")
        payload = run(module.report_bytes, data, fmt, preview_png=preview, repair_preview_png=repair_preview,
                      include_ai_analysis=app.config.get("reporting.include_ai_analysis"),
                      include_repair_evidence=app.config.get("reporting.include_repair_evidence"))
        content_type = "application/x-slai-sldc+zip" if fmt == "sldc" else "application/json"
        return file_response(payload, module.report_filename(data, fmt, prefix=prefix), content_type)

    return router


def main(argv=None) -> int:
    from pathlib import Path
    parser = argparse.ArgumentParser(description="CADCon 2D/3D CAD inspection and conversion")
    parser.add_argument("--slai-root", help="Path to the SLAI v2.4 checkout")
    parser.add_argument("--config", help="Path to a CADCon configuration YAML (default: configs/cadcon_config.yaml or $CADCON_CONFIG)")
    parser.add_argument("--data-dir", help="Local private CADCon data directory")
    parser.add_argument("--dwg-executable", help="Explicit absolute path to ODAFileConverter")
    parser.add_argument("--port", type=int, default=None,
                        help="Listening port (default from configuration, normally 8770, with automatic fallback when busy; 0 picks a free port)")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)
    root = _activate_slai(args.slai_root)
    if root is None:
        parser.error("SLAI shared tools were not found. Pass --slai-root or set SLAI_ROOT to your SLAI v2.4 checkout")
    from applications.tools.server import Identity, LocalServer, ServerOptions, serve
    from applications.tools.storage import default_data_dir
    loader = _module("utils.config_loader")
    cli = {}  # command-line flags are the highest-precedence overrides
    if args.port is not None:
        cli["server.port"] = args.port
    if args.data_dir:
        cli["paths.data_dir"] = args.data_dir
    if args.dwg_executable:
        cli["conversion.dwg_executable"] = args.dwg_executable
    if args.no_browser:
        cli["server.open_browser"] = False
    try:
        config = loader.activate(loader.load_config(args.config, overrides=cli), path=args.config, overrides=cli)
    except _module("utils.cadcon_errors").ConfigurationError as exc:
        parser.error(f"Configuration problem: {exc.message}")
    data_dir = Path(config.get("paths.data_dir")) if config.get("paths.data_dir") else default_data_dir("cadcon", "CADCon")
    if not APP_DIR.joinpath("dist", "index.html").is_file():
        parser.error("CADCon frontend has not been built. Run 'npm run build' from the CADCon directory")
    requested_port = config.get("server.port")
    app = CADCon(data_dir, None, root, config)
    token = secrets.token_urlsafe(32)
    options = ServerOptions(token, APP_DIR / "dist", host=config.get("server.host"), port=requested_port,
                            tools_web_dir=root / "applications" / "tools" / "web",
                            asset_dirs=(APP_DIR / "assets", root / "applications" / "assets"),
                            max_body_bytes=config.get("server.max_request_bytes"))
    router = build_router()
    identity = Identity("cadcon", "CADCon")
    try:
        server = LocalServer(app, identity, router, options)
    except OSError as exc:
        # A stale CADCon instance or another app may already be listening on the preferred port. Let the OS pick a free one; the server's
        # base_url below uses the *actual* assigned port. An explicit --port stays strict, as does server.port_fallback: false.
        address_busy = exc.errno == errno.EADDRINUSE or getattr(exc, "winerror", None) == 10048
        if args.port is not None or not address_busy or not config.get("server.port_fallback"):
            raise
        print(f"CADCon: port {requested_port} is occupied; selecting an available port.", file=sys.stderr)
        options.port = 0
        server = LocalServer(app, identity, router, options)
    details = [("Data", str(data_dir)), ("SLAI", str(root))]
    if config.get("application.startup_diagnostics"):
        diag = config.diagnostics()
        enabled = [k for k in loader.INTEGRATED_AGENTS if config.agent_enabled(k)]
        details += [("Config", f"{diag['loaded_from']} ({diag['file'] or 'built-in defaults'}), mode {config.get('application.mode')}"),
                    ("Repair", f"{config.get('repair.mode')} mode, up to {config.get('repair.max_candidates')} candidates"),
                    ("Agents", ", ".join(enabled) + " (loaded on first use)" if enabled else "none enabled")]
    return serve(server, title="SLAI CADCon", version=VERSION, url=f"{server.base_url}/?token={token}",
                 details=tuple(details), open_browser=config.get("server.open_browser"), on_close=app.close)


if __name__ == "__main__":
    raise SystemExit(main())
