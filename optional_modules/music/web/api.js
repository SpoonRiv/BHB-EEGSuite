export { getStatus } from '/web/js/api.js';

async function fetchJson(url, options) {
  const res = await fetch(url, options);
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch (_) {}
  if (!res.ok) {
    const msg = (data && (data.message || data.detail)) ? (data.message || data.detail) : `HTTP ${res.status}`;
    throw new Error(msg);
  }
  return data;
}

async function musicRequest(path, payload, timeout = 20000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);
  try {
    return await fetchJson(`/api/music/${path}`, {
      signal: controller.signal,
      ...(payload === undefined ? {} : {
        method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload),
      }),
    });
  } finally { clearTimeout(timer); }
}
export async function musicLibrary() { return musicRequest('library'); }
export async function musicLyrics(songId) { return musicRequest(`lyrics/${encodeURIComponent(songId)}`); }
export async function musicResults() { return musicRequest('results'); }
export async function musicExport(targets = [], bandpass = {}, results = []) { return musicRequest('export', {targets, bandpass, results}, 120000); }
export async function musicOpenExportFolder() { return musicRequest('open-export-folder', {}); }
export async function musicStart(payload) { return musicRequest('start', payload || {}); }
export async function musicHeartbeat(token) { return musicRequest('heartbeat', {token}, 5000); }
export async function musicTrialStart(token, index) { return musicRequest('trial/start', {token, index}); }
export async function musicEvent(token, index, event, timing) { return musicRequest('event', {token, index, event, ...(timing || {})}); }
export async function musicTrialStop(token, index, timing) { return musicRequest('trial/stop', {token, index, ...(timing || {})}, 120000); }
export async function musicStop(token, reason, timing) { return musicRequest('stop', {token, reason, ...(timing || {})}, 120000); }

