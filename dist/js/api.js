export class Api {
    async request(method, path, body) {
        const headers = { 'X-SLAI-Cadcon': '1' };
        if (body !== undefined)
            headers['Content-Type'] = 'application/json';
        const res = await fetch('/api' + path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body), credentials: 'same-origin' });
        const payload = await res.json();
        if (!res.ok || !payload.ok)
            throw new Error([payload.error?.message, payload.error?.hint].filter(Boolean).join(' ') || `Request failed (${res.status})`);
        return payload.data;
    }
    get(path) { return this.request('GET', path); }
    post(path, body) { return this.request('POST', path, body); }
    del(path) { return this.request('DELETE', path); }
    async upload(file) {
        const res = await fetch('/api/files/import?name=' + encodeURIComponent(file.name), {
            method: 'POST', credentials: 'same-origin', headers: { 'X-SLAI-Cadcon': '1', 'Content-Type': 'application/octet-stream' }, body: file,
        });
        const payload = await res.json();
        if (!res.ok || !payload.ok || !payload.data)
            throw new Error(payload.error?.message || 'Upload failed');
        return payload.data;
    }
    async postBlob(path, blob) {
        const res = await fetch('/api' + path, {
            method: 'POST', credentials: 'same-origin',
            headers: { 'X-SLAI-Cadcon': '1', 'Content-Type': blob.type || 'application/octet-stream' },
            body: blob,
        });
        const payload = await res.json();
        if (!res.ok || !payload.ok || !payload.data)
            throw new Error(payload.error?.message || 'Image upload failed');
        return payload.data;
    }
    async download(path, name) {
        const res = await fetch('/api' + path, { credentials: 'same-origin' });
        if (!res.ok)
            throw new Error('Download failed');
        const blob = await res.blob();
        const link = document.createElement('a');
        const url = URL.createObjectURL(blob);
        link.href = url;
        link.download = name;
        document.body.append(link);
        link.click();
        link.remove();
        window.setTimeout(() => URL.revokeObjectURL(url), 1500);
    }
}
