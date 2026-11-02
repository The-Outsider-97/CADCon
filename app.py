"""CADCon's thin composition root, using SLAI's shared local application server."""
from __future__ import annotations

import argparse
import importlib
import os
import secrets
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
    from src.utils.spatial_formats.common.errors import SpatialFormatError
    module = importlib.import_module(f"{__package__}.engine" if __package__ else "engine")
    router = Router()

    def run(action, *args, **kwargs):
        try:
            return action(*args, **kwargs)
        except module.GeometryError as exc:
            raise BadRequest(str(exc)) from exc
        except SpatialFormatError as exc:
            raise BadRequest(str(exc)) from exc
        except FileExistsError as exc:
            raise BadRequest("An output file with that name already exists") from exc
        except ImportError as exc:
            raise BadRequest(f"An optional CAD backend is unavailable: {exc}") from exc
        except ValueError as exc:
            raise BadRequest(str(exc)) from exc

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
        return {"id": identity, **result}

    @router.route("POST", "/api/analyze")
    def analyze(app, req):
        data = req.json()
        path = app.get_file(str(data.get("id", "")))
        report = run(module.analysis, path, unit=data.get("unit", "mm"))
        report["source"]["name"] = app.original_names.get(str(data.get("id")), path.name)
        identity = uuid.uuid4().hex
        with app.lock:
            app.reports[identity] = report
        return {"id": identity, "report": report}

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
        return file_response(path.read_bytes(), "cadcon-converted" + path.suffix, "application/octet-stream")

    @router.route("GET", "/api/reports/{id}")
    def report(app, req):
        with app.lock:
            data = app.reports.get(req.params["id"])
        if data is None:
            raise NotFound("Report not available")
        fmt = req.str_query("format", "json")
        payload = run(module.report_bytes, data, fmt)
        return file_response(payload, "cadcon-analysis." + fmt, "application/octet-stream")

    return router


def main(argv=None) -> int:
    from pathlib import Path
    parser = argparse.ArgumentParser(description="CADCon 2D/3D CAD inspection and conversion")
    parser.add_argument("--slai-root", help="Path to the SLAI v2.4 checkout")
    parser.add_argument("--data-dir", help="Local private CADCon data directory")
    parser.add_argument("--dwg-executable", help="Explicit absolute path to ODAFileConverter")
    parser.add_argument("--port", type=int, default=8770)
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
    if not 0 <= args.port <= 65535:
        parser.error("Port must be between 0 and 65535")
    app = CADCon(data_dir, args.dwg_executable, root)
    token = secrets.token_urlsafe(32)
    options = ServerOptions(token, APP_DIR / "dist", port=args.port,
                            tools_web_dir=root / "applications" / "tools" / "web",
                            asset_dirs=(APP_DIR / "assets", root / "applications" / "assets"),
                            max_body_bytes=64 * 1024 * 1024)
    server = LocalServer(app, Identity("cadcon", "CADCon"), build_router(), options)
    return serve(server, title="SLAI CADCon", version=VERSION, url=f"{server.base_url}/?token={token}",
                 details=(("Data", str(data_dir)), ("SLAI", str(root))), open_browser=not args.no_browser)


if __name__ == "__main__":
    raise SystemExit(main())
