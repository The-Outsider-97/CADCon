"""Exercise CADCon's shared-router API without a network listener."""
from __future__ import annotations

import io
import json
import struct

import pytest

import app as cadapp
from tests.test_engine import _png_bytes


def _call(router, state, method, path, *, body=None, query=None):
    from applications.tools.server.router import Request
    match = router.match(method, path)
    assert isinstance(match, tuple), f"Missing {method} route: {path}"
    action, params = match
    raw = body if isinstance(body, bytes) else json.dumps(body).encode('utf-8') if body is not None else b''
    request = Request(method, path, query or {}, {}, io.BytesIO(raw), len(raw) or None, params)
    return action(state, request)


def _binary_stl(triangles: int) -> bytes:
    facet = struct.pack('<12fH', 0.0, 0.0, 1.0,
                        0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0)
    return b'CADCon test'.ljust(80, b' ') + struct.pack('<I', triangles) + facet * triangles


def test_import_inspect_analysis_and_download(tmp_path, monkeypatch):
    root = __import__('pathlib').Path(__import__('src.utils.sgeo').utils.sgeo.__file__).parents[2]
    cadapp._activate_slai(str(root))
    state = cadapp.CADCon(tmp_path)
    routes = cadapp.build_router()
    model = b'v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n'
    uploaded = _call(routes, state, 'POST', '/api/files/import', body=model, query={'name': 'my triangle.obj'})
    identity = uploaded['id']
    assert uploaded['name'] == 'my triangle.obj'
    inspected = _call(routes, state, 'POST', '/api/inspect', body={'id': identity})
    assert inspected['source']['name'] == 'my triangle.obj'
    assert inspected['geometry_summary']['edges'] == 3

    report = _call(routes, state, 'POST', '/api/analyze', body={'id': identity})
    assert report['filenames']['sldc'] == 'cadcon-analysis-my triangle.sldc'
    response = _call(routes, state, 'GET', '/api/reports/' + report['id'], query={'format': 'json'})
    assert json.loads(response.body)['source']['name'] == 'my triangle.obj'
    assert response.download_name == 'cadcon-analysis-my triangle.json'
    with pytest.raises(Exception, match='model preview'):
        _call(routes, state, 'GET', '/api/reports/' + report['id'], query={'format': 'sldc'})
    png = _png_bytes(1600, 900)
    uploaded_png = _call(routes, state, 'POST', '/api/reports/' + report['id'] + '/preview',
                         body=png, query={'source_id': identity})
    assert uploaded_png['attached'] is True
    native_response = _call(routes, state, 'GET', '/api/reports/' + report['id'], query={'format': 'sldc'})
    assert native_response.download_name == 'cadcon-analysis-my triangle.sldc'
    from src.utils.sldc import loads_package
    assert len(loads_package(native_response.body).assets) == 1

    conversion = _call(routes, state, 'POST', '/api/convert', body={'id': identity, 'format': 'sgeo'})
    assert conversion['validated']
    assert conversion['source'] == 'my triangle.obj'
    assert conversion['target'] == 'my triangle.sgeo'
    result = _call(routes, state, 'GET', '/api/outputs/' + conversion['id'])
    assert result.body

    comparison = _call(routes, state, 'POST', '/api/compare', body={'left': identity, 'right': identity})
    assert comparison['status'] == 'identical'
    comp_report = _call(routes, state, 'GET', '/api/reports/' + comparison['report_id'], query={'format': 'sldc'})
    assert loads_package(comp_report.body).document['title'] == 'CADCon Model Comparison'

    deleted = _call(routes, state, 'DELETE', '/api/files/' + identity)
    assert deleted['removed']
    with pytest.raises(Exception):
        state.get_file(identity)


def test_stl_to_native_sgeo_via_api(tmp_path):
    state = cadapp.CADCon(tmp_path)
    router = cadapp.build_router()
    uploaded = _call(router, state, 'POST', '/api/files/import',
                     body=_binary_stl(64), query={'name': 'hinge.stl'})
    result = _call(router, state, 'POST', '/api/convert',
                   body={'id': uploaded['id'], 'format': 'sgeo', 'policy': 'best_effort', 'unit': 'mm'})
    assert result['validated'] is True
    assert result['target'] == 'hinge.sgeo'
    assert result['target_summary']['triangles'] == 64
    assert _call(router, state, 'GET', '/api/outputs/' + result['id']).body


def test_report_preview_rejects_invalid_image_and_wrong_source(tmp_path):
    state = cadapp.CADCon(tmp_path)
    router = cadapp.build_router()
    uploaded = _call(router, state, 'POST', '/api/files/import',
                     body=_binary_stl(1), query={'name': 'hinge.stl'})
    report = _call(router, state, 'POST', '/api/analyze', body={'id': uploaded['id']})
    endpoint = '/api/reports/' + report['id'] + '/preview'
    with pytest.raises(Exception, match='not associated'):
        _call(router, state, 'POST', endpoint, body=_png_bytes(), query={'source_id': 'other-file'})
    with pytest.raises(Exception, match='valid PNG'):
        _call(router, state, 'POST', endpoint, body=b'invalid', query={'source_id': uploaded['id']})
    assert report['id'] not in state.report_previews


def test_geometry_preview_caps_at_50k_triangles(tmp_path):
    state = cadapp.CADCon(tmp_path)
    router = cadapp.build_router()
    uploaded = _call(router, state, 'POST', '/api/files/import',
                     body=_binary_stl(50_003), query={'name': 'large.stl'})
    preview = _call(router, state, 'GET', '/api/files/' + uploaded['id'] + '/geometry')
    assert preview['kind'] == 'mesh'
    assert preview['preview_limit'] == 50_000
    assert preview['total_triangles'] == 50_003
    assert len(preview['faces']) == 50_000
    assert preview['partial'] is True
