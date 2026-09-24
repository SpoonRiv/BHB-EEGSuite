/*
Copyright (c) 2026 BUAA BHB. All rights reserved.

文件功能: 前端 API 封装（BLE 扫描/连接/模式控制/状态查询）
作者: Spoon
*/

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

export async function getConfig() {
  return fetchJson('/api/config');
}

export async function triggerStart() {
  return fetchJson('/api/trigger/start', { method: 'POST' });
}

export async function triggerStop() {
  return fetchJson('/api/trigger/stop', { method: 'POST' });
}

export async function getStatus() {
  return fetchJson('/api/status');
}

export async function getModules() { return fetchJson('/api/modules'); }
export async function refreshModules() { return fetchJson('/api/modules/refresh', { cache: 'no-store' }); }
export async function getModuleStatus(moduleId) { return fetchJson(`/api/modules/${encodeURIComponent(moduleId)}/status`, { cache: 'no-store' }); }
export async function installModule(moduleId) { return fetchJson(`/api/modules/${encodeURIComponent(moduleId)}/install`, { method: 'POST' }); }
export async function uninstallModule(moduleId) { return fetchJson(`/api/modules/${encodeURIComponent(moduleId)}/uninstall`, { method: 'POST' }); }

export async function bleDevices(timeoutSec = 3.0, whitelistOnly = true) {
  const qs = new URLSearchParams({
    timeout_sec: String(timeoutSec),
    whitelist_only: whitelistOnly ? 'true' : 'false',
  });
  return fetchJson(`/api/ble/devices?${qs.toString()}`);
}

export async function bleConnect(address, name) {
  return fetchJson('/api/ble/connect', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ address: address || null, name: name || null }),
  });
}

export async function bleDisconnect() {
  return fetchJson('/api/ble/disconnect', { method: 'POST' });
}

export async function appShutdown() {
  return fetchJson('/api/app/shutdown', { method: 'POST' });
}

export async function appMinimize() {
  return fetchJson('/api/app/minimize', { method: 'POST' });
}

export async function modeSelect(mode) {
  return fetchJson('/api/mode/select', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ mode }),
  });
}

export async function modeStart(mode) {
  return fetchJson('/api/mode/start', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ mode }),
  });
}

export async function modeStop(mode) {
  return fetchJson('/api/mode/stop', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ mode }),
  });
}

export async function getDebugEvents(limit = 200) {
  const qs = new URLSearchParams({ limit: String(limit) });
  return fetchJson(`/api/debug/events?${qs.toString()}`);
}

export async function offlineExport(payload) {
  return fetchJson('/api/offline/export', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload || {}),
  });
}

export async function offlineSession(sessionId) {
  const sid = String(sessionId || '').trim();
  const qs = new URLSearchParams({ session_id: sid });
  return fetchJson(`/api/offline/session?${qs.toString()}`);
}

export async function offlineOpenFolder(sessionId) {
  const sid = String(sessionId || '').trim();
  const qs = new URLSearchParams({ session_id: sid });
  return fetchJson(`/api/offline/open-folder?${qs.toString()}`, { method: 'POST' });
}

export async function eegChannelOptions() {
  return fetchJson('/api/eeg/channel/options');
}

export async function eegChannelGetSelection() {
  return fetchJson('/api/eeg/channel/selection');
}

export async function eegChannelSetSelection(payload) {
  return fetchJson('/api/eeg/channel/selection', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload || {}),
  });
}

export async function eegChannelApply() {
  return fetchJson('/api/eeg/channel/apply', { method: 'POST' });
}

export async function eegChannelPresetUpsertLocal(payload) {
  return fetchJson('/api/eeg/channel/presets/local', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload || {}),
  });
}

export async function eegChannelPresetDeleteLocal(name) {
  return fetchJson('/api/eeg/channel/presets/local/delete', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name: name || '' }),
  });
}

export async function postModeStart(mode) {
  const res = await fetch('/api/mode/start', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ mode })
  });
  return res.json();
}

export async function postModeStop(mode) {
  const res = await fetch('/api/mode/stop', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ mode })
  });
  return res.json();
}

export async function getTwoLevelCommands() {
  return fetchJson('/api/control/commands');
}

export async function sendTwoLevelCommand(l1, l2, data) {
  return fetchJson('/api/control/send', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ l1, l2, data: data || null }),
  });
}

export async function getSignalBandpass() {
  return fetchJson('/api/signal/bandpass');
}

export async function setSignalBandpass(payload) {
  return fetchJson('/api/signal/bandpass', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload || {}),
  });
}

export async function getSignalQuality() {
  return fetchJson('/api/signal/quality');
}

export async function setSignalQuality(payload) {
  return fetchJson('/api/signal/quality', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload || {}),
  });
}

export async function getSignalQualitySnapshot() {
  return fetchJson('/api/signal/quality/snapshot');
}

export async function setSignalQualityManualChannels(channelNames) {
  return fetchJson('/api/signal/quality/manual', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ bad_channels: Array.isArray(channelNames) ? channelNames : [] }),
  });
}
