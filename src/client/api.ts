export interface Uploaded { id: string; name: string; size_bytes: number; format: string }
export interface Summary { source: { name: string; format: string; size_bytes: number; sha256: string }; geometry_summary: Record<string, unknown> }
export interface Analysis extends Summary { report_kind: string; findings: Array<{ code: string; severity: string; description: string }> }
export interface Conversion { id: string; source: string; target: string; validated: boolean; warnings: string[]; target_summary: Record<string, unknown>; backend: string; duration_ms: number; source_sha256: string; target_sha256: string; backend_report?: Record<string, unknown>; fidelity?: { passed: boolean; violations: string[]; checks: Array<{ name: string; status: string; note?: string }> } }
export interface Comparison { report_id: string; status: string; left: Summary; right: Summary; differences: Record<string, { left: unknown; right: unknown }>; note?: string; reason?: string; surface_distance?: Record<string, unknown>; bounding_box_delta?: Record<string, unknown>; registration?: Registration }
export type Preview = { kind: 'mesh'; vertices: number[][]; faces: number[][]; new_faces?: number[]; partial: boolean; total_triangles: number; preview_limit: number } | { kind: 'svg'; markup: string };
export interface RepairValidation { boundary_edges: number; nonmanifold_edges: number; inconsistent_winding_edges: number; manifold_face_components: number; topologically_closed: boolean; watertight: boolean; solid_validity: string; status: string; triangles: number; vertices: number }
export interface AgentCheck { agent: string; operation: string; status: string; assessment: string | null; reason: string | null; limitations: string[]; evidence: Array<{ kind: string; claim: string }>; duration_ms: number }
export interface RepairCandidate { id: string; strategy: string; label: string; explanation: string; added_faces: number; validation: RepairValidation; agent_checks?: AgentCheck[] }
export interface RepairDiagnosis { source: RepairValidation; reconstructed: RepairValidation; tolerance: number; tolerance_mode?: 'automatic' | 'manual'; recommendation?: { tolerance: number; unit: string; model_diagonal: number; evaluated_levels: number; baseline_boundary_edges: number; expected_boundary_edges: number; expected_nonmanifold_edges: number; reason: string }; coincident_records: number; boundary_loops: Array<{edges: number; perimeter: number}>; interpretation: string; validation_limitations: string[] }
export interface RepairResult { diagnosis: RepairDiagnosis; candidates: RepairCandidate[]; agents: {status: string; reason?: string; interpretation?: string; agent?: string} }
export interface AgentStatus { key: string; enabled: boolean; class?: string; state: string; reason?: string; loaded_at?: string; operations?: string[]; cooldown_seconds?: number; last_failure?: string; metrics?: Record<string, number> }
export interface SystemInfo {
  version: string; python: string; mode: string; dwg_backend: boolean; uploaded_files: number; outputs: number;
  limits: Record<string, number>; configuration: { loaded_from: string; file: string | null; sources: Record<string, number>; overridden: string[]; notes: string[] };
  repair: Record<string, unknown>; cache: { hits: number; misses: number; entries: number; capacity: number };
  agents: { enabled: boolean; integrations: AgentStatus[]; workflows: Record<string, string[]>; metrics: { inflight: number; abandoned_calls: number }; not_integrated: string };
}
export interface Registration { status: string; reason?: string | null; result?: { transform: unknown; rmse: number; iterations?: number; converged?: boolean }; assessment?: string | null; samples?: number; unit?: string; direction?: string; limitations?: string[] }

export class Api {
  private async request<T>(method: string, path: string, body?: unknown): Promise<T> {
    const headers: Record<string, string> = { 'X-SLAI-Cadcon': '1' };
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    const res = await fetch('/api' + path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body), credentials: 'same-origin' });
    const payload = await res.json() as { ok?: boolean; data?: T; error?: { message?: string; hint?: string } };
    if (!res.ok || !payload.ok) throw new Error([payload.error?.message, payload.error?.hint].filter(Boolean).join(' ') || `Request failed (${res.status})`);
    return payload.data as T;
  }
  get<T>(path: string): Promise<T> { return this.request<T>('GET', path); }
  post<T>(path: string, body: unknown): Promise<T> { return this.request<T>('POST', path, body); }
  del<T>(path: string): Promise<T> { return this.request<T>('DELETE', path); }
  async upload(file: File): Promise<Uploaded> {
    const res = await fetch('/api/files/import?name=' + encodeURIComponent(file.name), {
      method: 'POST', credentials: 'same-origin', headers: { 'X-SLAI-Cadcon': '1', 'Content-Type': 'application/octet-stream' }, body: file,
    });
    const payload = await res.json() as { ok: boolean; data?: Uploaded; error?: { message: string } };
    if (!res.ok || !payload.ok || !payload.data) throw new Error(payload.error?.message || 'Upload failed');
    return payload.data;
  }
  async postBlob<T>(path: string, blob: Blob): Promise<T> {
    const res = await fetch('/api' + path, {
      method: 'POST', credentials: 'same-origin',
      headers: { 'X-SLAI-Cadcon': '1', 'Content-Type': blob.type || 'application/octet-stream' },
      body: blob,
    });
    const payload = await res.json() as { ok: boolean; data?: T; error?: { message?: string } };
    if (!res.ok || !payload.ok || !payload.data) throw new Error(payload.error?.message || 'Image upload failed');
    return payload.data;
  }
  async download(path: string, name: string): Promise<void> {
    const res = await fetch('/api' + path, { credentials: 'same-origin' });
    if (!res.ok) throw new Error('Download failed');
    const blob = await res.blob();
    const link = document.createElement('a');
    const url = URL.createObjectURL(blob);
    link.href = url; link.download = name; document.body.append(link); link.click(); link.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 1500);
  }
}
