"""CADCon's thin composition root, using SLAI's shared local application server."""
from __future__ import annotations

import argparse
import errno
import hashlib
import importlib
import os
import secrets
import struct
import sys
import threading
import uuid
from pathlib import Path
from typing import Any

APP_DIR = Path(__file__).resolve().parent
VERSION = "1.0.0-dev"


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
    def __init__(self, data_dir: Path, dwg_executable: str | None = None, slai_root: Path | None = None) -> None:
        self.data_dir = data_dir
        self.upload_dir = data_dir / "uploads"
        self.output_dir = data_dir / "outputs"
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.dwg_executable = dwg_executable
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
        self.lock = threading.RLock()

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
    agent_bridge = importlib.import_module(f"{__package__}.slai.bridge.agents" if __package__ else "slai.bridge.agents")
    router = Router()

    def run(action, *args, **kwargs):
        try:
            return action(*args, **kwargs)
        except module.GeometryError as exc:
            raise BadRequest(str(exc)) from exc
        except SpatialFormatError as exc:
            raise BadRequest(str(exc)) from exc
        except SLDCError as exc:
            raise BadRequest(f"The native SLDC report is invalid: {exc}") from exc
        except FileExistsError as exc:
            raise BadRequest("An output file with that name already exists") from exc
        except ImportError as exc:
            raise BadRequest(f"An optional CAD backend is unavailable: {exc}") from exc
        except ValueError as exc:
            raise BadRequest(str(exc)) from exc

    def read_png_preview(req):
        from src.utils import sldc
        if not req.length or not 0 < req.length <= 8 * 1024 * 1024:
            raise LimitExceeded("PNG preview must be between 1 byte and 8 MiB")
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

    @router.route("GET", "/api/health")
    def health(app, req):
        return {"application": "CADCon", "version": VERSION, "status": "ready"}

    @router.route("GET", "/api/capabilities")
    def capabilities(app, req):
        return module.capabilities(dwg_executable=app.dwg_executable)

    @router.route("GET", "/api/system")
    def system(app, req):
        return {"version": VERSION, "python": sys.version.split()[0],
                "slai_root": str(app.slai_root) if app.slai_root else None,
                "dwg_backend": bool(app.dwg_executable),
                "uploaded_files": len(app.files), "outputs": len(app.outputs),
                "agents": {"spatial_query": "opt-in via SLAI SpatialQueries",
                           "orchestrated_agent_workflows": "not_connected"}}

    @router.route("POST", "/api/files/import")
    def upload(app, req):
        if not req.length or not 0 < req.length <= module.MAX_SOURCE_BYTES:
            raise LimitExceeded("File is empty or exceeds the 64 MiB limit")
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
        result = run(module.inspect, path, unit=data.get("unit", "mm"), dwg_executable=app.dwg_executable)
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
        result = run(module.convert, source, path, policy=data.get("policy", "best_effort"),
                     unit=data.get("unit", "mm"), dwg_executable=app.dwg_executable)
        source_name = app.original_names.get(str(data.get("id")), source.name)
        result["source"] = source_name
        result["target"] = Path(source_name).stem + ext
        with app.lock:
            app.outputs[identity] = path
            app.output_names[identity] = result["target"]
        return {"id": identity, **result}

    @router.route("GET", "/api/repair/capabilities")
    def repair_capabilities(app, req):
        return repairs.capability_info()

    @router.route("POST", "/api/repair/analyze")
    def repair_analyze(app, req):
        data = req.json()
        source_id = str(data.get("id", ""))
        path = app.get_file(source_id)
        tolerance = data.get("tolerance", 0)
        if not isinstance(tolerance, (float, int)):
            raise BadRequest("Tolerance must be a number")
        mesh = run(module.load_mesh, path, unit=data.get("unit", "mm"))
        diagnosis = run(repairs.diagnose, mesh, float(tolerance))
        with app.lock:
            app.repair_diagnoses[source_id] = diagnosis
        return {"diagnosis": diagnosis, "capabilities": repairs.capability_info()}

    @router.route("POST", "/api/repair/suggest")
    def repair_suggest(app, req):
        data = req.json()
        source_id = str(data.get("id", ""))
        path = app.get_file(source_id)
        tol = data.get("tolerance", 0)
        limit = data.get("max_candidates", 5)
        agent_enabled = data.get("use_agents", True)
        if not isinstance(tol, (float, int)) or not isinstance(limit, int):
            raise BadRequest("Tolerance and candidate count must be numeric")
        if not isinstance(agent_enabled, bool):
            raise BadRequest("use_agents must be a Boolean")
        mesh = run(module.load_mesh, path, unit=data.get("unit", "mm"))
        diagnosis = run(repairs.diagnose, mesh, float(tol))
        if len(mesh.faces) > 200_000:
            limit = min(limit, 2)  # Bound the in-memory candidate working set.
        generated = run(repairs.candidates, mesh, float(tol), limit)
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
        repair_result["agents"] = agent_bridge.mesh_repair_advice(diagnosis, outputs, enabled=agent_enabled)
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
        report = run(module.analysis, path, unit=data.get("unit", "mm"))
        report["source"]["name"] = app.original_names.get(str(data.get("id")), path.name)
        identity = uuid.uuid4().hex
        with app.lock:
            app.reports[identity] = report
            app.report_sources[identity] = str(data.get("id"))
        return {"id": identity, "report": report,
                "filenames": {fmt: module.report_filename(report, fmt) for fmt in ("json", "sldc")}}

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
        image, width, height = read_png_preview(req)
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
        image, width, height = read_png_preview(req)
        with app.lock:
            if app.report_sources.get(report_id) != entry[0]:
                raise BadRequest("The report changed during repair preview generation")
            app.report_repair_previews[report_id] = image
            app.reports[report_id].setdefault("repair", {})["selected_preview"] = {
                "candidate_id": candidate_id, "format": "png", "width": width, "height": height,
                "sha256": hashlib.sha256(image).hexdigest()}
        return {"attached": True, "width": width, "height": height}

    @router.route("POST", "/api/compare")
    def compare(app, req):
        data = req.json()
        a = app.get_file(str(data.get("left", "")))
        b = app.get_file(str(data.get("right", "")))
        try:
            tol = float(data.get("tolerance", 0.000001))
        except (ValueError, TypeError) as exc:
            raise BadRequest("Tolerance must be a number") from exc
        result = run(module.compare, a, b, tolerance=tol, unit=data.get("unit", "mm"))
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
        return run(module.spatial_nearest, source, data.get("point_m", [0, 0, 0]), unit=data.get("unit", "mm"))

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
        payload = run(module.report_bytes, data, fmt, preview_png=preview,
                      repair_preview_png=repair_preview)
        content_type = "application/x-slai-sldc+zip" if fmt == "sldc" else "application/json"
        return file_response(payload, module.report_filename(data, fmt), content_type)

    return router


def main(argv=None) -> int:
    from pathlib import Path
    parser = argparse.ArgumentParser(description="CADCon 2D/3D CAD inspection and conversion")
    parser.add_argument("--slai-root", help="Path to the SLAI v2.4 checkout")
    parser.add_argument("--data-dir", help="Local private CADCon data directory")
    parser.add_argument("--dwg-executable", help="Explicit absolute path to ODAFileConverter")
    parser.add_argument("--port", type=int, default=None,
                        help="Listening port (default 8770, with automatic fallback when busy; 0 picks a free port)")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)
    root = _activate_slai(args.slai_root)
    if root is None:
        parser.error("SLAI shared tools were not found. Pass --slai-root or set SLAI_ROOT to your SLAI v2.4 checkout")
    from applications.tools.server import Identity, LocalServer, ServerOptions, serve
    from applications.tools.storage import default_data_dir
    data_dir = Path(args.data_dir) if args.data_dir else default_data_dir("cadcon", "CADCon")
    if not APP_DIR.joinpath("dist", "index.html").is_file():
        parser.error("CADCon frontend has not been built. Run 'npm run build' from the CADCon directory")
    requested_port = 8770 if args.port is None else args.port
    if not 0 <= requested_port <= 65535:
        parser.error("Port must be between 0 and 65535")
    app = CADCon(data_dir, args.dwg_executable, root)
    token = secrets.token_urlsafe(32)
    options = ServerOptions(token, APP_DIR / "dist", port=requested_port,
                            tools_web_dir=root / "applications" / "tools" / "web",
                            asset_dirs=(APP_DIR / "assets", root / "applications" / "assets"),
                            max_body_bytes=64 * 1024 * 1024)
    router = build_router()
    identity = Identity("cadcon", "CADCon")
    try:
        server = LocalServer(app, identity, router, options)
    except OSError as exc:
        # A stale CADCon instance or another app may already be listening on
        # the default port. Let the OS pick a free one; the server's base_url
        # below uses the *actual* assigned port. Explicit ports remain strict.
        address_busy = exc.errno == errno.EADDRINUSE or getattr(exc, "winerror", None) == 10048
        if args.port is not None or not address_busy:
            raise
        print("CADCon: default port 8770 is occupied; selecting an available port.", file=sys.stderr)
        options.port = 0
        server = LocalServer(app, identity, router, options)
    return serve(server, title="SLAI CADCon", version=VERSION, url=f"{server.base_url}/?token={token}",
                 details=(("Data", str(data_dir)), ("SLAI", str(root))), open_browser=not args.no_browser)


if __name__ == "__main__":
    raise SystemExit(main())
