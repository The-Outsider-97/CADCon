import { Api, type Uploaded, type Summary, type Analysis, type Conversion, type Comparison, type Preview } from './api.js';

const api = new Api();
type Page = 'counter' | 'converter' | 'analysis' | 'compare' | 'system';
const NAV: Array<[Page, string, string]> = [
  ['counter', 'Counter', '◫'], ['converter', 'Converter', '⇄'],
  ['analysis', 'Analysis & Report', '▤'], ['compare', 'Compare', '◈'],
];
const root = document.getElementById('app')!;
const files: Uploaded[] = [];
let page: Page = 'counter';
let busy = false;
let selected = '';
let systemData: Record<string, unknown> | null = null;
let capabilities: { formats: Record<string, { available: boolean; import: boolean; export: boolean; backend: string; limitations: string[] }>; routes: Record<string, string[]>; conditional: Record<string, string> } | null = null;
let viewerDisposers: Array<() => void> = [];
const UNITS = ['mm', 'cm', 'm', 'in', 'ft'];
let assumedUnit = UNITS.includes(localStorage.getItem('cadcon-unit') || '') ? localStorage.getItem('cadcon-unit')! : 'mm';

function el<K extends keyof HTMLElementTagNameMap>(name: K, className = '', text?: string): HTMLElementTagNameMap[K] {
  const node = document.createElement(name);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
function button(label: string, onClick: () => void, cls = ''): HTMLButtonElement {
  const node = el('button', 'btn ' + cls, label);
  node.type = 'button'; node.addEventListener('click', onClick); return node;
}
function notice(message: string, bad = false): void {
  const n = el('div', 'toast-msg' + (bad ? ' bad' : ''), message);
  document.body.append(n); window.setTimeout(() => n.remove(), 5000);
}
async function action<T>(promise: () => Promise<T>): Promise<T | undefined> {
  if (busy) return undefined;
  busy = true; root.classList.add('working');
  try { return await promise(); }
  catch (error) { notice(error instanceof Error ? error.message : String(error), true); return undefined; }
  finally { busy = false; root.classList.remove('working'); }
}
function heading(title: string, sub: string): HTMLElement {
  const h = el('div', 'workspace-heading'); h.append(el('h1', '', title), el('p', 'muted', sub)); return h;
}
function card(title?: string): HTMLElement {
  const node = el('section', 'panel'); if (title) node.append(el('h2', 'panel-title', title)); return node;
}
function selector(placeholder = 'Select a file', value = selected): HTMLSelectElement {
  const node = el('select', 'select'); node.setAttribute('aria-label', placeholder);
  node.append(new Option(placeholder, ''));
  for (const file of files) node.append(new Option(file.name, file.id));
  node.value = value; return node;
}
function declaredUnit(onChange?: () => void): HTMLElement {
  const wrapper = el('label', 'field'); wrapper.append(el('span', '', 'Declared units (STL/OBJ input)'));
  const control = el('select', 'select'); control.setAttribute('aria-label', 'Unit assumption');
  for (const unit of UNITS) control.append(new Option(unit, unit));
  control.value = assumedUnit;
  control.addEventListener('change', () => {
    assumedUnit = control.value; localStorage.setItem('cadcon-unit', assumedUnit); onChange?.();
  });
  wrapper.append(control);
  return wrapper;
}
function stat(label: string, value: unknown): HTMLElement {
  const node = el('div', 'stat'); node.append(el('span', 'stat-label', label), el('strong', '', value === null || value === undefined ? '—' : String(value)));
  return node;
}
function pairs(values: Record<string, unknown>): HTMLElement {
  const table = el('dl', 'details');
  for (const [key, value] of Object.entries(values)) {
    if (Array.isArray(value) || value !== null && typeof value === 'object') continue;
    table.append(el('dt', '', key.replaceAll('_', ' ')), el('dd', '', value === null || value === undefined ? 'Unavailable' : String(value)));
  }
  return table;
}
function uploadBox(done: (file: Uploaded) => void): HTMLElement {
  const wrap = el('div', 'drop-zone');
  const input = el('input'); input.type = 'file'; input.accept = '.stl,.obj,.glb,.sgeo,.sdrw,.dxf,.dwg,.svg,.ifc,.step,.stp,.fbx,.sat'; input.hidden = true;
  const label = el('strong', '', 'Drop CAD files here'); const hint = el('span', 'muted', 'or click to browse · 64 MiB maximum');
  wrap.append(el('div', 'drop-icon', '↥'), label, hint, input);
  wrap.tabIndex = 0; wrap.setAttribute('role', 'button'); wrap.setAttribute('aria-label', 'Upload CAD file');
  wrap.addEventListener('click', () => input.click());
  wrap.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); input.click(); } });
  const process = (file?: File): void => {
    if (!file) return;
    void action(async () => {
      const added = await api.upload(file); files.push(added); selected = added.id; done(added); notice(`${file.name} imported`);
    });
  };
  input.addEventListener('change', () => process(input.files?.[0]));
  wrap.addEventListener('dragover', e => { e.preventDefault(); wrap.classList.add('dragover'); });
  wrap.addEventListener('dragleave', () => wrap.classList.remove('dragover'));
  wrap.addEventListener('drop', e => { e.preventDefault(); wrap.classList.remove('dragover'); process(e.dataTransfer?.files[0]); });
  return wrap;
}
function fileSelectRow(onChange: (id: string) => void, value = selected): HTMLElement {
  const row = el('div', 'file-toolbar'); const sel = selector('Choose imported model', value);
  sel.addEventListener('change', () => { selected = sel.value; onChange(sel.value); });
  row.append(sel, button('Import file', () => (document.querySelector('.drop-zone input') as HTMLInputElement | null)?.click(), 'btn-outline'));
  return row;
}
function resultBlock(parent: HTMLElement, summary: Summary): void {
  const section = card('Geometry statistics'); const g = summary.geometry_summary;
  const grid = el('div', 'stats-grid');
  for (const key of ['vertices', 'edges', 'polygons', 'triangles', 'entities', 'layers', 'watertight']) {
    if (key in g) grid.append(stat(key.replaceAll('_', ' '), g[key]));
  }
  section.append(grid, pairs(g));
  const source = card('Source'); source.append(pairs(summary.source));
  parent.append(section, source);
  if (Array.isArray(g.warnings) && g.warnings.length) parent.append(el('div', 'notice warning', g.warnings.join(' · ')));
}

function viewer(parent: HTMLElement, fileId: string): void {
  const container = card('Model preview'); container.classList.add('viewer-card');
  const status = el('div', 'muted', 'Loading preview…'); container.append(status); parent.append(container);
  void api.get<Preview>(`/files/${fileId}/geometry`).then(preview => {
    if (!container.isConnected) return;
    container.replaceChildren(el('h2', 'panel-title', preview.kind === 'mesh' ? '3D geometry' : '2D drawing'));
    if (preview.kind === 'svg') {
      const frame = el('div', 'viewer-surface svg-viewer');
      const img = el('img'); const url = URL.createObjectURL(new Blob([preview.markup], { type: 'image/svg+xml' }));
      img.src = url; img.alt = 'CAD drawing preview'; frame.append(img); container.append(frame);
      viewerDisposers.push(() => URL.revokeObjectURL(url));
    } else {
      const canvas = el('canvas', 'viewer-surface') as HTMLCanvasElement;
      canvas.setAttribute('aria-label', 'Interactive 3D model preview; drag to orbit and scroll to zoom');
      container.append(canvas);
      startCanvasPreview(canvas, preview);
      if (preview.partial) container.append(el('p', 'notice warning', `Preview limited to ${preview.preview_limit.toLocaleString()} of ${preview.total_triangles.toLocaleString()} triangles; analysis uses the complete file.`));
      container.append(el('p', 'muted viewer-tip', 'Drag to orbit · Scroll to zoom · Double-click to reset'));
    }
  }).catch(e => { status.textContent = e instanceof Error ? e.message : 'Preview unavailable'; });
}

function meshPainter(preview: Extract<Preview, { kind: 'mesh' }>) {
  const points = preview.vertices;
  // Large STL previews can contain 150k distinct vertex records. Avoid
  // Math.min(...vertices), which overflows browser argument limits at this size.
  const minimum = [Infinity, Infinity, Infinity];
  const maximum = [-Infinity, -Infinity, -Infinity];
  for (const point of points) {
    for (let axis = 0; axis < 3; axis++) {
      minimum[axis] = Math.min(minimum[axis], point[axis]);
      maximum[axis] = Math.max(maximum[axis], point[axis]);
    }
  }
  const center = minimum.map((value, axis) => (value + maximum[axis]) / 2);
  const span = Math.max(1e-6, ...minimum.map((value, axis) => maximum[axis] - value));
  return (ctx: CanvasRenderingContext2D, w: number, h: number, yaw: number, pitch: number,
          zoom: number, interactive: boolean, pixelRatio: number, report = false): void => {
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.fillStyle = report ? '#f7f8fb' : '#15191f';
    ctx.fillRect(0, 0, w, h);
    if (!points.length) return;
    const cosYaw = Math.cos(yaw), sinYaw = Math.sin(yaw), cosPitch = Math.cos(pitch), sinPitch = Math.sin(pitch);
    const screen = new Float32Array(points.length * 3);
    const scale = Math.min(w, h) * zoom;
    for (let i = 0; i < points.length; i++) {
      const p = points[i];
      const x = (p[0] - center[0]) / span, y = (p[1] - center[1]) / span, z = (p[2] - center[2]) / span;
      const xr = cosYaw * x - sinYaw * z, zr = sinYaw * x + cosYaw * z;
      const offset = i * 3;
      screen[offset] = w / 2 + xr * scale;
      screen[offset + 1] = h / 2 - (cosPitch * y - sinPitch * zr) * scale;
      screen[offset + 2] = sinPitch * y + cosPitch * zr;
    }
    const stride = interactive ? Math.max(1, Math.ceil(preview.faces.length / 8_000)) : 1;
    const sorted: Array<{ index: number; z: number }> = [];
    for (let index = 0; index < preview.faces.length; index += stride) {
      const [a, b, c] = preview.faces[index];
      sorted.push({ index, z: (screen[a * 3 + 2] + screen[b * 3 + 2] + screen[c * 3 + 2]) / 3 });
    }
    sorted.sort((a, b) => a.z - b.z);
    ctx.lineWidth = Math.max(0.5, pixelRatio * 0.5);
    for (const { index } of sorted) {
      const [a, b, c] = preview.faces[index];
      const ax = screen[a * 3], ay = screen[a * 3 + 1];
      const bx = screen[b * 3], by = screen[b * 3 + 1];
      const cx = screen[c * 3], cy = screen[c * 3 + 1];
      ctx.beginPath(); ctx.moveTo(ax, ay); ctx.lineTo(bx, by); ctx.lineTo(cx, cy); ctx.closePath();
      const cross = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax);
      ctx.fillStyle = cross >= 0 ? 'rgba(214,179,0,.65)' : 'rgba(130,148,170,.70)';
      ctx.fill();
      if (!interactive) { ctx.strokeStyle = report ? 'rgba(6,12,22,.24)' : 'rgba(6,12,22,.26)'; ctx.stroke(); }
    }
  };
}

async function captureModelPreview(fileId: string): Promise<Blob> {
  const preview = await api.get<Preview>(`/files/${fileId}/geometry`);
  const canvas = document.createElement('canvas');
  canvas.width = 1600; canvas.height = 900;
  const ctx = canvas.getContext('2d');
  if (!ctx) throw new Error('This browser cannot render PNG report previews');
  if (preview.kind === 'mesh') {
    meshPainter(preview)(ctx, canvas.width, canvas.height, .8, -.45, 1.35, false, 1, true);
  } else {
    ctx.fillStyle = '#f7f8fb'; ctx.fillRect(0, 0, canvas.width, canvas.height);
    const image = new Image();
    const url = URL.createObjectURL(new Blob([preview.markup], { type: 'image/svg+xml' }));
    try {
      image.src = url;
      await image.decode();
      const scale = Math.min(1440 / image.naturalWidth, 810 / image.naturalHeight);
      if (!Number.isFinite(scale) || scale <= 0) throw new Error('Drawing preview has invalid dimensions');
      const w = image.naturalWidth * scale, h = image.naturalHeight * scale;
      ctx.drawImage(image, (1600 - w) / 2, (900 - h) / 2, w, h);
    } finally { URL.revokeObjectURL(url); }
  }
  return await new Promise<Blob>((resolve, reject) => canvas.toBlob(
    blob => blob ? resolve(blob) : reject(new Error('Failed to capture the model preview')), 'image/png'));
}

function startCanvasPreview(canvas: HTMLCanvasElement, preview: Extract<Preview, { kind: 'mesh' }>): void {
  const ctx = canvas.getContext('2d'); if (!ctx) return;
  const paint = meshPainter(preview);
  let yaw = 0.8, pitch = -0.45, zoom = 1.0, drag = false, lastX = 0, lastY = 0;
  let frame = 0, wheelSettled = 0;
  const render = (interactive: boolean): void => {
    const box = canvas.getBoundingClientRect();
    const pixelRatio = Math.min(2, window.devicePixelRatio || 1);
    const w = canvas.width = Math.max(240, Math.round(box.width * pixelRatio));
    const h = canvas.height = Math.max(160, Math.round(box.height * pixelRatio));
    paint(ctx, w, h, yaw, pitch, zoom, interactive, pixelRatio);
  };
  const schedule = (interactive: boolean): void => {
    if (frame) window.cancelAnimationFrame(frame);
    frame = window.requestAnimationFrame(() => {
      frame = 0;
      if (canvas.isConnected) render(interactive);
    });
  };
  canvas.addEventListener('pointerdown', e => { drag = true; lastX = e.clientX; lastY = e.clientY; canvas.setPointerCapture(e.pointerId); });
  canvas.addEventListener('pointerup', () => { drag = false; schedule(false); });
  canvas.addEventListener('pointercancel', () => { drag = false; schedule(false); });
  canvas.addEventListener('pointermove', e => {
    if (!drag) return;
    yaw += (e.clientX - lastX) * .012; pitch = Math.max(-1.5, Math.min(1.5, pitch + (e.clientY - lastY) * .012));
    lastX = e.clientX; lastY = e.clientY; schedule(true);
  });
  canvas.addEventListener('wheel', e => {
    e.preventDefault(); zoom = Math.max(.25, Math.min(8, zoom * (e.deltaY > 0 ? .9 : 1.1)));
    schedule(true); window.clearTimeout(wheelSettled);
    wheelSettled = window.setTimeout(() => schedule(false), 180);
  }, { passive: false });
  canvas.addEventListener('dblclick', () => { yaw = .8; pitch = -.45; zoom = 1; schedule(false); });
  const resize = new ResizeObserver(() => schedule(false));
  resize.observe(canvas);
  viewerDisposers.push(() => { resize.disconnect(); window.cancelAnimationFrame(frame); window.clearTimeout(wheelSettled); });
  schedule(false);
}

function counter(main: HTMLElement): void {
  main.append(heading('Geometry Counter', 'Inspect topology, dimensions and model statistics'));
  main.append(uploadBox(() => show('counter')));
  const area = el('div', 'workspace-grid'); main.append(area);
  const row = fileSelectRow(id => { if (id) show('counter'); }); row.append(declaredUnit(() => show('counter'))); main.append(row);
  if (!selected) { area.append(el('div', 'empty', 'Import a 2D or 3D CAD file to inspect it.')); return; }
  const stats = el('div', 'detail-stack'); area.append(stats);
  viewer(area, selected);
  void api.post<Summary>('/inspect', { id: selected, unit: assumedUnit }).then(data => {
    if (stats.isConnected) resultBlock(stats, data);
  }).catch(e => notice(e.message, true));
}

function converter(main: HTMLElement): void {
  main.append(heading('CAD Converter', 'Convert formats with explicit fidelity policies'));
  main.append(uploadBox(() => show('converter')));
  const panel = card('Conversion settings'); const chosen = selector();
  chosen.addEventListener('change', () => { selected = chosen.value; refreshTargets(); });
  const target = el('select', 'select'); target.setAttribute('aria-label', 'Target format');
  const explanation = el('p', 'muted');
  const refreshTargets = (): void => {
    const format = files.find(f => f.id === chosen.value)?.format || '';
    const available = capabilities?.routes[format] || [];
    target.replaceChildren();
    for (const name of available) target.append(new Option(name.toUpperCase(), name));
    target.disabled = !available.length;
    explanation.textContent = !format ? 'Choose a source to see verified output routes.' : available.length
      ? `Available routes from ${format.toUpperCase()}; strict policies may restrict lossy operations.`
      : `No verified conversion route is available from ${format.toUpperCase()}. Check System for backend requirements.`;
  };
  refreshTargets();
  const policy = el('select', 'select'); policy.setAttribute('aria-label', 'Conversion policy');
  for (const v of ['best_effort', 'strict', 'lossless_where_supported', 'allow_approximation']) policy.append(new Option(v.replaceAll('_', ' '), v));
  const fields = el('div', 'form-grid');
  for (const [label, control] of [['Source file', chosen], ['Target format', target], ['Conversion policy', policy]] as Array<[string, HTMLElement]>) {
    const wrapper = el('label', 'field'); wrapper.append(el('span', '', label), control); fields.append(wrapper);
  }
  fields.append(declaredUnit());
  const output = el('div');
  const go = button('Convert file', () => {
    if (!chosen.value || !target.value) { notice('Choose a source and an available output format', true); return; }
    void action(async () => {
      go.disabled = true;
      const sourceName = files.find(file => file.id === chosen.value)?.name || 'the selected file';
      output.replaceChildren(el('div', 'notice', `Converting ${sourceName} to ${target.value.toUpperCase()}… large meshes can take a while. Please wait for validation.`));
      try {
        const res = await api.post<Conversion>('/convert', { id: chosen.value, format: target.value, policy: policy.value, unit: assumedUnit });
        output.replaceChildren(el('div', 'notice success', `Converted and validated: ${res.source} → ${res.target}`));
        output.append(el('p', 'muted', `${res.backend} · ${res.duration_ms} ms · SHA-256: ${res.target_sha256.slice(0, 18)}…`));
        if (res.warnings.length) output.append(el('div', 'notice warning', res.warnings.join(' · ')));
        const download = () => api.download('/outputs/' + res.id, res.target);
        output.append(button('Download ' + res.target, () => { void download().catch(e => notice(e.message, true)); }, 'btn-primary'));
        try {
          await download();
          output.append(el('p', 'muted', 'Browser download initiated. Use the button above if it was blocked.'));
        } catch (error) {
          output.append(el('div', 'notice warning', `Conversion succeeded, but the download could not start: ${error instanceof Error ? error.message : String(error)}`));
        }
      } catch (error) {
        output.replaceChildren(el('div', 'notice bad', `Conversion failed: ${error instanceof Error ? error.message : String(error)}`));
      } finally {
        go.disabled = false;
      }
    });
  }, 'btn-primary');
  panel.append(fields, explanation, go, output); main.append(panel);
  main.append(el('p', 'muted', 'Conversions not implemented or missing their verified backend are rejected; source files are never overwritten.'));
}

function analysisPage(main: HTMLElement): void {
  main.append(heading('Analysis & Report', 'Deterministic geometry checks and native SLDC reporting'));
  main.append(uploadBox(() => show('analysis')));
  const panel = card('Analyze model'); const chosen = selector(); chosen.addEventListener('change', () => { selected = chosen.value; });
  const output = el('div', 'detail-stack');
  panel.append(chosen, declaredUnit(), button('Run analysis', () => {
    if (!chosen.value) { notice('Choose a file first', true); return; }
    void action(async () => {
      const fileId = chosen.value;
      output.replaceChildren(el('div', 'notice', 'Analyzing geometry and preparing the report…'));
      const result = await api.post<{ id: string; report: Analysis; filenames: { json: string; sldc: string } }>(
        '/analyze', { id: fileId, unit: assumedUnit });
      output.replaceChildren(); resultBlock(output, result.report);
      const actions = el('div', 'file-toolbar');
      actions.append(button('Export JSON', () => {
        void api.download(`/reports/${result.id}?format=json`, result.filenames.json).catch(e => notice(e.message, true));
      }, 'btn-outline'));
      const sldcButton = button('Export SLDC', () => {
        void api.download(`/reports/${result.id}?format=sldc`, result.filenames.sldc).catch(e => notice(e.message, true));
      }, 'btn-primary');
      sldcButton.disabled = true;
      actions.append(sldcButton);
      output.prepend(actions);
      const previewStatus = el('p', 'muted', 'Rendering the actual model preview for the SLDC cover…');
      actions.after(previewStatus);
      const prepare = async (): Promise<void> => {
        const picture = await captureModelPreview(fileId);
        await api.postBlob(`/reports/${result.id}/preview?source_id=${encodeURIComponent(fileId)}`, picture);
        const triangles = Number(result.report.geometry_summary.triangles || 0);
        previewStatus.textContent = triangles > 50_000
          ? 'Model PNG embedded. The cover preview uses at most 50,000 triangles; geometric analysis uses the full model.'
          : 'Model PNG embedded for native SLDC export.';
        sldcButton.disabled = false;
      };
      try { await prepare(); }
      catch (error) {
        previewStatus.textContent = `Model screenshot unavailable: ${error instanceof Error ? error.message : String(error)}. JSON export remains available.`;
        previewStatus.classList.add('notice', 'warning');
        const retry = button('Retry screenshot', () => {
          void action(async () => {
            retry.disabled = true;
            try { await prepare(); retry.remove(); previewStatus.classList.remove('notice', 'warning'); }
            finally { retry.disabled = false; }
          });
        }, 'btn-outline');
        previewStatus.after(retry);
      }
    });
  }, 'btn-primary'), output); main.append(panel);
  const spatial = card('SLAI Spatial queries');
  spatial.append(el('p', 'muted', 'Opt-in nearest-object query using the actual SLAI Spatial subsystem. Coordinates are in meters; distance is measured to entity centers.'));
  const inputs = [0, 1, 2].map(axis => {
    const input = el('input', 'input'); input.type = 'number'; input.step = '.1'; input.value = '0'; input.setAttribute('aria-label', ['X coordinate (m)', 'Y coordinate (m)', 'Z coordinate (m)'][axis]); return input;
  });
  const coords = el('div', 'form-grid'); for (const input of inputs) coords.append(input);
  const response = el('div');
  spatial.append(coords, button('Find nearest entity', () => {
    if (!chosen.value) { notice('Select a model first', true); return; }
    void action(async () => {
      const result = await api.post<{ backend: string; query: { entity_ids: string[]; distances: number[] }; entity_count: number; meaning: string }>('/spatial/nearest', {
        id: chosen.value, point_m: inputs.map(input => Number(input.value)), unit: assumedUnit,
      });
      response.replaceChildren(el('div', 'notice success', `Nearest: ${result.query.entity_ids[0] || 'no entity'} · ${result.query.distances[0]?.toFixed(4) || '—'} m`), el('p', 'muted', result.meaning));
    });
  }, 'btn-outline'), response);
  main.append(spatial);
}

function comparePage(main: HTMLElement): void {
  main.append(heading('Model Compare', 'Detect grounded differences between two compatible CAD files'));
  main.append(uploadBox(() => show('compare')));
  const panel = card('Files and tolerance'); const left = selector('First file'); const right = selector('Second file', '');
  if (files.length > 1) right.value = files.find(f => f.id !== left.value)?.id || '';
  const tol = el('input', 'input'); tol.type = 'number'; tol.min = '0'; tol.max = '1'; tol.step = '0.000001'; tol.value = '0.000001';
  const fields = el('div', 'form-grid');
  for (const [name, field] of [['First model', left], ['Second model', right], ['Tolerance', tol]] as Array<[string, HTMLElement]>) {
    const wrapper = el('label', 'field'); wrapper.append(el('span', '', name), field); fields.append(wrapper);
  }
  fields.append(declaredUnit());
  const output = el('div');
  panel.append(fields, button('Compare models', () => {
    if (!left.value || !right.value) { notice('Select two models', true); return; }
    void action(async () => {
      const result = await api.post<Comparison>('/compare', { left: left.value, right: right.value, tolerance: Number(tol.value), unit: assumedUnit });
      const detail = card(`Result: ${result.status.replaceAll('_', ' ')}`);
      if (result.reason || result.note) detail.append(el('p', 'muted', result.reason || result.note));
      if (Object.keys(result.differences).length) detail.append(pairs(Object.fromEntries(Object.entries(result.differences).map(([k, v]) => [k, `${JSON.stringify(v.left)} → ${JSON.stringify(v.right)}`]))));
      else detail.append(el('p', '', 'No differences found in the measured summary fields.'));
      const actions = el('div', 'file-toolbar');
      actions.append(button('Export JSON', () => { void api.download(`/reports/${result.report_id}?format=json`, 'cadcon-comparison.json').catch(e => notice(e.message, true)); }, 'btn-outline'));
      actions.append(button('Export SLDC', () => { void api.download(`/reports/${result.report_id}?format=sldc`, 'cadcon-comparison.sldc').catch(e => notice(e.message, true)); }, 'btn-primary'));
      const previews = el('div', 'workspace-grid compare-previews');
      viewer(previews, left.value); viewer(previews, right.value);
      output.replaceChildren(detail, actions, previews);
    });
  }, 'btn-primary'), output);
  main.append(panel, el('p', 'muted', 'Comparing equal counts is not proof of geometric equivalence. Detailed surface registration is not yet implemented.'));
}

function systemPage(main: HTMLElement): void {
  main.append(heading('System', 'Conversion backends, SLAI integration and application diagnostics'));
  const grid = el('div', 'workspace-grid'); main.append(grid);
  const system = card('Application status'); const formats = card('Format capabilities'); grid.append(system, formats);
  if (systemData) {
    system.append(pairs(systemData));
    const agents = systemData.agents;
    if (agents && typeof agents === 'object' && !Array.isArray(agents)) {
      system.append(el('h2', 'panel-title', 'SLAI integration'), pairs(agents as Record<string, unknown>));
    }
  }
  if (capabilities) {
    const list = el('div', 'capability-list');
    for (const [name, entry] of Object.entries(capabilities.formats)) {
      const item = el('div', 'capability-row'); item.append(el('strong', '', name.toUpperCase()), el('span', entry.available ? `Import · ${entry.export ? 'export' : 'read-only'}` : 'Unavailable'));
      item.title = [entry.backend, ...entry.limitations].join(' · ');
      item.classList.add(entry.available ? 'ready' : 'disabled'); list.append(item);
    }
    formats.append(list, el('p', 'muted', 'Availability does not imply every format pair can be converted.'));
  }
  const settings = card('Preferences');
  settings.append(el('p', 'muted', 'Theme and default unit assumptions are stored locally in this browser. Use the sidebar toggle for dark/light mode.'));
  settings.append(declaredUnit());
  main.append(settings);
}

function show(next: Page): void {
  page = next; history.replaceState(null, '', '#/' + next);
  for (const dispose of viewerDisposers) dispose(); viewerDisposers = [];
  const main = document.querySelector<HTMLElement>('main.workspace')!;
  main.replaceChildren();
  for (const item of document.querySelectorAll<HTMLElement>('.nav-item')) {
    const active = item.dataset.page === next;
    item.classList.toggle('active', active);
    if (active) item.setAttribute('aria-current', 'page'); else item.removeAttribute('aria-current');
  }
  if (next === 'counter') counter(main);
  if (next === 'converter') converter(main);
  if (next === 'analysis') analysisPage(main);
  if (next === 'compare') comparePage(main);
  if (next === 'system') systemPage(main);
}

function themeToggle(): HTMLElement {
  const current = localStorage.getItem('cadcon-theme') === 'light' ? 'light' : 'dark';
  document.documentElement.dataset.theme = current;
  const toggle = button('', () => {
    const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = next; localStorage.setItem('cadcon-theme', next); sync();
  }, 'theme-toggle');
  toggle.setAttribute('role', 'switch');
  const sync = (): void => { const dark = document.documentElement.dataset.theme === 'dark'; toggle.textContent = dark ? '☾  Dark mode' : '☀  Light mode'; toggle.setAttribute('aria-checked', String(dark)); };
  sync(); return toggle;
}

const sidebar = el('aside', 'sidebar');
const brand = el('div', 'brand'); const logo = el('img', 'brand-logo'); logo.src = '/assets/cadcon_main.png'; logo.alt = 'CADCon logo';
const brandText = el('div'); brandText.append(el('strong', '', 'CADCon'), el('span', 'muted', 'SLAI CAD workspace')); brand.append(logo, brandText);
const nav = el('nav', 'navigation'); nav.setAttribute('aria-label', 'Workspaces');
for (const [id, label, icon] of NAV) {
  const b = button(`${icon}   ${label}`, () => show(id), 'nav-item'); b.dataset.page = id; nav.append(b);
}
const spacer = el('div', 'spacer');
const system = button('⚙   System', () => show('system'), 'nav-item subtle'); system.dataset.page = 'system';
sidebar.append(brand, nav, spacer, system, themeToggle());
const workspace = el('main', 'workspace'); root.replaceChildren(sidebar, workspace);

const initial = location.hash.slice(2) as Page;
show([...NAV.map(n => n[0]), 'system'].includes(initial) ? initial : 'counter');
window.addEventListener('hashchange', () => { const p = location.hash.slice(2) as Page; if ([...NAV.map(n => n[0]), 'system'].includes(p)) show(p); });
window.addEventListener('dragover', e => { if (!(e.target as HTMLElement).closest('.drop-zone')) e.preventDefault(); });
window.addEventListener('drop', e => { if (!(e.target as HTMLElement).closest('.drop-zone')) e.preventDefault(); });
void Promise.all([api.get<typeof systemData>('/system'), api.get<typeof capabilities>('/capabilities'), api.get<Uploaded[]>('/files')]).then(([s, c, available]) => {
  systemData = s; capabilities = c;
  files.splice(0, files.length, ...available);
  if (!selected) selected = files[0]?.id || '';
  show(page);
}).catch(e => notice(e instanceof Error ? e.message : String(e), true));
