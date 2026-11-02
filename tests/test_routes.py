"""Exercise CADCon's shared-router API without a network listener."""
from __future__ import annotations

import io
import json

import pytest

import app as cadapp


def _call(router, state, method, path, *, body=None, query=None):
    from applications.tools.server.router import Request
    match = router.match(method, path)
    assert isinstance(match, tuple), f"Missing {method} route: {path}"
    action, params = match
    raw = body if isinstance(body, bytes) else json.dumps(body).encode('utf-8') if body is not None else b''
    request = Request(method, path, query or {}, {}, io.BytesIO(raw), len(raw) or None, params)
    return action(state, request)


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
    response = _call(routes, state, 'GET', '/api/reports/' + report['id'], query={'format': 'json'})
    assert json.loads(response.body)['source']['name'] == 'my triangle.obj'

    conversion = _call(routes, state, 'POST', '/api/convert', body={'id': identity, 'format': 'sgeo'})
    assert conversion['validated']
    assert conversion['source'] == 'my triangle.obj'
    assert conversion['target'] == 'my triangle.sgeo'
    result = _call(routes, state, 'GET', '/api/outputs/' + conversion['id'])
    assert result.body

    comparison = _call(routes, state, 'POST', '/api/compare', body={'left': identity, 'right': identity})
    assert comparison['status'] == 'identical'
    comp_report = _call(routes, state, 'GET', '/api/reports/' + comparison['report_id'], query={'format': 'sldc'})
    from src.utils.sldc import loads_package
    assert loads_package(comp_report.body).document['title'] == 'CADCon Model Comparison'

    deleted = _call(routes, state, 'DELETE', '/api/files/' + identity)
    assert deleted['removed']
    with pytest.raises(Exception):
        state.get_file(identity)
