"""End-to-end: launch CADCon through the SLAI launcher and drive its HTTP API with real files.

Needs a SLAI v2.4 checkout (``SLAI_ROOT``) with CADCon at ``SLAI/applications/CADCon`` and a built client (``npm run build``); skipped, with the
reason, when either is missing. Run with ``py -m pytest -q tests_e2e``.
"""
from __future__ import annotations

import http.cookiejar
import json
import math
import os
import re
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parents[1]
CUBE = (b"v 0 0 0\nv 1 0 0\nv 1 1 0\nv 0 1 0\nv 0 0 1\nv 1 0 1\nv 1 1 1\nv 0 1 1\n"
        b"f 1 4 3 2\nf 5 6 7 8\nf 1 2 6 5\nf 2 3 7 6\nf 3 4 8 7\nf 4 1 5 8\n")


def _slai_root() -> Path | None:
    candidates = [Path(os.environ["SLAI_ROOT"])] if os.environ.get("SLAI_ROOT") else []
    candidates += list(APP_DIR.parents)
    return next((c for c in candidates if (c / "applications" / "tools" / "server" / "http.py").is_file()), None)


class Client:
    def __init__(self, url: str) -> None:
        parts = urllib.parse.urlsplit(url)
        self.base = f"{parts.scheme}://{parts.netloc}"
        self.launch = url
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def call(self, method: str, path: str, body=None, *, raw: bool = False):
        headers = {"X-SLAI-Cadcon": "1"}
        data = body
        if isinstance(body, dict):
            data, headers["Content-Type"] = json.dumps(body).encode(), "application/json"
        elif isinstance(body, bytes):
            headers["Content-Type"] = "application/octet-stream"
        try:
            with self.opener.open(urllib.request.Request(self.base + path, data=data, method=method, headers=headers), timeout=60) as response:
                payload, status = response.read(), response.status
        except urllib.error.HTTPError as error:
            payload, status = error.read(), error.code
        if raw:
            return status, payload
        parsed = json.loads(payload or b"null")
        return status, (parsed["data"] if isinstance(parsed, dict) and parsed.get("ok") is True and "data" in parsed else parsed)


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    root = _slai_root()
    if root is None:
        pytest.skip("SLAI v2.4 checkout not found (set SLAI_ROOT)")
    if not (APP_DIR / "dist" / "index.html").is_file():
        pytest.skip("client not built (npm run build)")
    if (root / "applications" / "CADCon").resolve() != APP_DIR.resolve():
        pytest.skip("CADCon must be checked out at SLAI/applications/CADCon for the launcher test")
    env = dict(os.environ, SLAI_ROOT=str(root), PYTHONPATH=str(root), PYTHONUNBUFFERED="1")
    process = subprocess.Popen([sys.executable, "-m", "applications.CADCon", "--no-browser", "--port", "0",
                                "--data-dir", str(tmp_path_factory.mktemp("data"))],
                               cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    url, seen, deadline = None, [], time.time() + 60
    while url is None and time.time() < deadline:
        line = process.stdout.readline()
        if not line:
            break
        seen.append(line)
        found = re.search(r"http://127\.0\.0\.1:\d+/\?token=[\w-]+", line)
        url = found.group(0) if found else None
    if url is None:
        process.kill()
        pytest.fail("CADCon did not print its launch URL:\n" + "".join(seen)[-1500:])
    client = Client(url)
    assert client.call("GET", "/?token=" + url.split("token=")[1], raw=True)[0] == 200   # exchanges the token for the session cookie
    yield client
    process.terminate()
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()


def test_requests_without_the_session_are_refused(server) -> None:
    anonymous = Client(server.launch)
    assert anonymous.call("GET", "/api/system", raw=True)[0] in (401, 403)


def test_shared_startup_loader_is_published_and_wired(server) -> None:
    status, page = server.call("GET", "/", raw=True)
    html = page.decode()
    assert status == 200 and "startup/loader.js" in html and 'data-name="CADCon"' in html and "startup/loader.css" in html
    for asset in ("/startup/loader.js", "/startup/loader.css", "/assets/cadcon_main.png", "/js/main.js"):
        status, body = server.call("GET", asset, raw=True)
        assert status == 200 and body, asset
    assert b"slai:ready" in server.call("GET", "/js/main.js", raw=True)[1]


def test_import_inspect_convert_and_round_trip_every_advertised_3d_route(server) -> None:
    _, caps = server.call("GET", "/api/capabilities")
    status, uploaded = server.call("POST", "/api/files/import?name=cube.obj", CUBE)
    assert status == 200
    status, inspected = server.call("POST", "/api/inspect", {"id": uploaded["id"], "unit": "mm"})
    summary = inspected["geometry_summary"]
    assert (summary["triangles"], summary["watertight"]) == (12, True)
    assert math.isclose(summary["surface_area"], 6.0, rel_tol=1e-9) and math.isclose(summary["volume"], 1.0, rel_tol=1e-9)
    routes = caps["routes"]["obj"]
    assert routes, "OBJ advertises no conversion route"
    for target in routes:
        status, converted = server.call("POST", "/api/convert", {"id": uploaded["id"], "format": target, "unit": "mm"})
        assert status == 200 and converted["status"] == "completed" and converted["validated"] and converted["fidelity_verified"], (target, converted)
        status, blob = server.call("GET", "/api/outputs/" + converted["id"], raw=True)
        assert status == 200 and blob
        status, again = server.call("POST", f"/api/files/import?name=roundtrip.{target}", blob)
        status, compared = server.call("POST", "/api/compare", {"left": uploaded["id"], "right": again["id"], "unit": "mm", "tolerance": 1e-6})
        assert compared["status"] in {"identical", "geometrically_equivalent"}, (target, compared["status"])


def test_reports_are_non_empty_and_describe_the_measured_model(server) -> None:
    _, uploaded = server.call("POST", "/api/files/import?name=report.obj", CUBE)
    status, report = server.call("POST", "/api/analyze", {"id": uploaded["id"], "unit": "mm"})
    assert status == 200
    status, blob = server.call("GET", "/api/reports/" + report["id"] + "?format=json", raw=True)
    parsed = json.loads(blob)
    assert status == 200 and parsed["geometry_summary"]["triangles"] == 12 and parsed["source"]["sha256"]


@pytest.mark.parametrize("name,payload", [("empty.obj", b""), ("junk.stl", b"\x00" * 10), ("nan.obj", b"v nan 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n"),
                                          ("range.obj", b"v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 9\n"),
                                          ("count.stl", b"x" * 80 + struct.pack("<I", 0xFFFFFFFF))])
def test_malformed_models_fail_cleanly_without_a_traceback(server, name: str, payload: bytes) -> None:
    status, response = server.call("POST", "/api/files/import?name=" + name, payload)
    if status == 200:
        status, response = server.call("POST", "/api/inspect", {"id": response["id"]})
    assert status in (400, 413, 415, 422) and "Traceback" not in json.dumps(response)


def test_unsupported_target_is_refused_not_faked(server) -> None:
    _, uploaded = server.call("POST", "/api/files/import?name=refuse.obj", CUBE)
    status, response = server.call("POST", "/api/convert", {"id": uploaded["id"], "format": "step"})
    assert status == 400 and response["ok"] is False
