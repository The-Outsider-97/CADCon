"""Exercise CADCon's shared-router API without a network listener."""
from __future__ import annotations

import io
import errno
import json
import struct

import pytest

import app as cadapp
from tests.test_engine import _png_bytes


def test_launcher_recovers_from_occupied_default_port(tmp_path, monkeypatch, capsys):
    from applications.tools import server as shared_server

    (tmp_path / 'dist').mkdir()
    (tmp_path / 'dist' / 'index.html').write_text('<html></html>', encoding='utf-8')
    monkeypatch.setattr(cadapp, 'APP_DIR', tmp_path)
    monkeypatch.setattr(cadapp, '_activate_slai', lambda _root: tmp_path)
    monkeypatch.setattr(cadapp, 'build_router', lambda: object())
    ports = []
    served = []

    class FakeServer:
        def __init__(self, _app, _identity, _router, options):
            ports.append(options.port)
            if options.port == 8770:
                raise OSError(errno.EADDRINUSE, 'Address already in use')
            self.base_url = 'http://127.0.0.1:49200'

    monkeypatch.setattr(shared_server, 'LocalServer', FakeServer)
    monkeypatch.setattr(shared_server, 'serve', lambda _server, **kwargs: served.append(kwargs) or 0)
    args = ['--data-dir', str(tmp_path / 'data'), '--no-browser']
    assert cadapp.main(args) == 0
    assert ports == [8770, 0]
    assert served[0]['url'].startswith('http://127.0.0.1:49200/')
    assert 'default port 8770 is occupied' in capsys.readouterr().err

    # A user-selected fixed port must never be switched silently.
    ports.clear()
    with pytest.raises(OSError):
        cadapp.main([*args, '--port', '8770'])
    assert ports == [8770]


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
    assert len(loads_package(native_response.body).assets) == 2  # Logo + source screenshot.

    conversion = _call(routes, state, 'POST', '/api/convert', body={'id': identity, 'format': 'sgeo'})
    assert conversion['validated']
    assert conversion['source'] == 'my triangle.obj'
    assert conversion['target'] == 'my triangle.sgeo'
    result = _call(routes, state, 'GET', '/api/outputs/' + conversion['id'])
    assert result.body

    comparison = _call(routes, state, 'POST', '/api/compare', body={'left': identity, 'right': identity})
    assert comparison['status'] == 'identical'
    with pytest.raises(Exception, match='both comparison model previews'):
        _call(routes, state, 'GET', '/api/reports/' + comparison['report_id'], query={'format': 'sldc'})
    for side in ('left', 'right'):
        assert _call(routes, state, 'POST', '/api/reports/' + comparison['report_id'] + '/comparison-preview',
                     query={'side': side, 'source_id': identity}, body=png)['attached'] is True
    comp_report = _call(routes, state, 'GET', '/api/reports/' + comparison['report_id'], query={'format': 'sldc'})
    comp_package = loads_package(comp_report.body)
    assert comp_package.document['title'] == 'CADCon Model Comparison'
    assert len(comp_package.assets) == 2  # Same model image twice, correctly deduplicated.
    assert len([span for block in comp_package.document['blocks'] for span in block.get('spans', [])
                if span['type'] == 'image']) == 2

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


def test_comparison_images_require_correct_source_side_and_survive_sldc_roundtrip(tmp_path):
    state = cadapp.CADCon(tmp_path)
    router = cadapp.build_router()
    left = _call(router, state, 'POST', '/api/files/import',
                 body=b'v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n', query={'name': 'bracket-before.obj'})
    right = _call(router, state, 'POST', '/api/files/import',
                  body=b'v 0 0 0\nv 2 0 0\nv 0 1 0\nf 1 2 3\n', query={'name': 'bracket-after.obj'})
    comparison = _call(router, state, 'POST', '/api/compare',
                       body={'left': left['id'], 'right': right['id']})
    endpoint = '/api/reports/' + comparison['report_id']
    assert json.loads(_call(router, state, 'GET', endpoint, query={'format': 'json'}).body)['left']['source']['name'] == 'bracket-before.obj'
    with pytest.raises(Exception, match='both comparison model previews'):
        _call(router, state, 'GET', endpoint, query={'format': 'sldc'})
    with pytest.raises(Exception, match='does not belong'):
        _call(router, state, 'POST', endpoint + '/comparison-preview',
              query={'side': 'right', 'source_id': left['id']}, body=_png_bytes())
    for side, identity, size in (('left', left['id'], 1600), ('right', right['id'], 1400)):
        image = _png_bytes(size, 900)
        response = _call(router, state, 'POST', endpoint + '/comparison-preview',
                         query={'side': side, 'source_id': identity}, body=image)
        assert response['attached'] is True
    from src.utils import sldc
    package = sldc.loads_package(_call(router, state, 'GET', endpoint, query={'format': 'sldc'}).body)
    assert len(package.assets) == 3  # Logo and both distinct embedded previews.
    assert [b['text'] for b in package.document['blocks'] if b['type'] == 'heading'][-2:] == ['Differences', 'Provenance']
    image_row = next(b for b in package.document['blocks'] if b.get('text') == '\uFFFC  \uFFFC')
    assert len([span for span in image_row['spans'] if span['type'] == 'image']) == 2
    assert package.document['metadata']['comparison_source_sha256']['left'] == state.reports[comparison['report_id']]['left']['source']['sha256']


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
