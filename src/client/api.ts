export interface Uploaded { id: string; name: string; size_bytes: number; format: string }
export interface Summary { source: { name: string; format: string; size_bytes: number; sha256: string }; geometry_summary: Record<string, unknown> }
export interface Analysis extends Summary { report_kind: string; findings: Array<{ code: string; severity: string; description: string }> }
export interface Conversion { id: string; source: string; target: string; validated: boolean; warnings: string[]; target_summary: Record<string, unknown>; backend: string; duration_ms: number; source_sha256: string; target_sha256: string; backend_report?: Record<string, unknown> }
export interface Comparison { report_id: string; status: string; left: Summary; right: Summary; differences: Record<string, { left: unknown; right: unknown }>; note?: string; reason?: string }
export type Preview = { kind: 'mesh'; vertices: number[][]; faces: number[][]; partial: boolean; total_triangles: number; preview_limit: number } | { kind: 'svg'; markup: string };

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
