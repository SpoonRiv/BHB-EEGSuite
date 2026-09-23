/*
Copyright (c) 2026 BUAA BHB. All rights reserved.

文件功能: 离线存储页面逻辑（停止采集后导出 CSV/EDF，可选带通滤波另存）
作者: Spoon
*/

import { getConfig, offlineExport, offlineOpenFolder, offlineSession } from './api.js';
import { navigate } from './router.js';

let pageActive = false;
let lastSessionId = '';

function getLastSessionId() {
  try { return sessionStorage.getItem('bhb_last_eeg_session') || ''; } catch (_) {}
  return '';
}

function getLastSessionDir() {
  try { return sessionStorage.getItem('bhb_last_eeg_session_dir') || ''; } catch (_) {}
  return '';
}

function setStatus(text, kind) {
  const box = document.getElementById('offline-status');
  if (!box) return;
  box.classList.remove('success', 'error');
  if (kind === 'success') box.classList.add('success');
  else box.classList.add('error');
  const value = String(text || '').trim();
  box.textContent = value.startsWith('状态：') ? value : `状态：${value || '等待操作'}`;
}

function setOpenFolderButtonVisible(visible) {
  const btn = document.getElementById('btn-offline-open-folder');
  if (!btn) return;
  btn.style.display = visible ? '' : 'none';
  btn.disabled = !visible;
}

function setMetricsVisible(visible) {
  const el = document.getElementById('offline-metrics');
  if (!el) return;
  el.style.display = visible ? '' : 'none';
}

function setMetricValue(id, value) {
  const el = document.getElementById(id);
  if (el) el.textContent = value || '--';
}

function setMetricsData({ samples, channels, size, duration }) {
  const el = document.getElementById('offline-metrics');
  const message = document.getElementById('offline-metrics-message');
  if (!el) return;
  el.classList.remove('has-message');
  if (message) {
    message.style.display = 'none';
    message.textContent = '';
  }
  setMetricValue('offline-metric-samples', samples);
  setMetricValue('offline-metric-channels', channels);
  setMetricValue('offline-metric-size', size);
  setMetricValue('offline-metric-duration', duration);
}

function setMetricsMessage(text) {
  const el = document.getElementById('offline-metrics');
  const message = document.getElementById('offline-metrics-message');
  if (!el || !message) return;
  el.classList.add('has-message');
  message.textContent = text || '';
  message.style.display = '';
}

function setSessionInfo(session) {
  const dirEl = document.getElementById('offline-session-dir');
  if (dirEl) {
    const path = session && session.session_dir ? String(session.session_dir) : '--';
    dirEl.textContent = path;
    dirEl.title = path === '--' ? '' : path;
  }
}

function setFilterVisible(enabled) {
  const blocks = [
    document.getElementById('offline-filter-block'),
    document.getElementById('offline-filtered-block'),
  ];
  const toggle = document.getElementById('offline-filter-enable');
  for (const block of blocks) {
    if (!block) continue;
    block.setAttribute('aria-disabled', String(!enabled));
    block.querySelectorAll('input').forEach(input => { input.disabled = !enabled; });
  }
  if (toggle) toggle.setAttribute('aria-expanded', String(enabled));
}

function buildTargets() {
  const rawCsv = !!document.getElementById('offline-raw-csv')?.checked;
  const rawEdf = !!document.getElementById('offline-raw-edf')?.checked;
  const filterEnabled = !!document.getElementById('offline-filter-enable')?.checked;
  const filCsv = filterEnabled && !!document.getElementById('offline-fil-csv')?.checked;
  const filEdf = filterEnabled && !!document.getElementById('offline-fil-edf')?.checked;
  const out = [];
  if (rawCsv) out.push({ kind: 'raw', fmt: 'csv' });
  if (rawEdf) out.push({ kind: 'raw', fmt: 'edf' });
  if (filCsv) out.push({ kind: 'filtered', fmt: 'csv' });
  if (filEdf) out.push({ kind: 'filtered', fmt: 'edf' });
  return out;
}

function readNumber(id, fallback) {
  const el = document.getElementById(id);
  if (!el) return fallback;
  const v = Number(el.value);
  if (!Number.isFinite(v)) return fallback;
  return v;
}

function setDefaultsFromConfig(cfg) {
  const offline = cfg && cfg.offline ? cfg.offline : null;
  const fd = offline && offline.filter_defaults ? offline.filter_defaults : null;
  const low = fd && typeof fd.lowcut_hz_default === 'number' ? fd.lowcut_hz_default : 3.0;
  const high = fd && typeof fd.highcut_hz_default === 'number' ? fd.highcut_hz_default : 50.0;

  const lowEl = document.getElementById('offline-lowcut');
  const highEl = document.getElementById('offline-highcut');
  if (lowEl) lowEl.value = String(low);
  if (highEl) highEl.value = String(high);
}

function fmtSeconds(sec) {
  const v = Number(sec);
  if (!Number.isFinite(v) || v < 0) return '--';
  if (v < 60) return `${v.toFixed(2)}s`;
  const m = Math.floor(v / 60);
  const s = v - m * 60;
  return `${m}m ${s.toFixed(1)}s`;
}

function fmtInt(v) {
  const n = Number(v);
  if (!Number.isFinite(n)) return '--';
  return String(Math.trunc(n));
}

function fmtMiB(miB) {
  const v = Number(miB);
  if (!Number.isFinite(v) || v < 0) return '--';
  return v.toFixed(2);
}

function getEegChannelCount(session, dataChannelCount) {
  const names = session && Array.isArray(session.channel_names) ? session.channel_names : [];
  if (names.length) {
    const eegNames = names.filter((name) => !/^(trig|trigger|marker)$/i.test(String(name || '').trim()));
    if (eegNames.length === 8 || eegNames.length === 16) return eegNames.length;
  }
  const count = Number(dataChannelCount);
  if (count === 9 || count === 17) return count - 1;
  return Number.isFinite(count) ? count : null;
}

function tryGetCachedSessionMeta(sid) {
  try {
    const raw = sessionStorage.getItem('bhb_last_eeg_session_meta') || '';
    if (!raw) return null;
    const obj = JSON.parse(raw);
    if (!obj || typeof obj !== 'object') return null;
    if (obj.session_id && String(obj.session_id) === String(sid)) return obj;
  } catch (_) {}
  return null;
}

function cacheSessionMeta(obj) {
  try { sessionStorage.setItem('bhb_last_eeg_session_meta', JSON.stringify(obj || {})); } catch (_) {}
}

async function loadAndRenderMetrics(sessionId) {
  if (!sessionId) {
    setMetricsVisible(false);
    return;
  }
  const cached = tryGetCachedSessionMeta(sessionId);
  if (cached) {
    const sr = cached.sampling_rate_hz ? Number(cached.sampling_rate_hz) : null;
    const dataChannels = Array.isArray(cached.channel_names) ? cached.channel_names.length : null;
    const eegChannels = getEegChannelCount(cached, dataChannels);
    const samples = cached.total_samples ? Number(cached.total_samples) : null;
    const dataSec = (sr && samples && sr > 0) ? (samples / sr) : null;
    const estMiB = (samples && dataChannels) ? ((samples * dataChannels * 4) / (1024.0 * 1024.0)) : null;
    setMetricsData({
      samples: `${fmtInt(samples)} 点`,
      channels: `${fmtInt(eegChannels)} 通道`,
      size: `${estMiB !== null ? fmtMiB(estMiB) : '--'} MiB`,
      duration: dataSec !== null ? fmtSeconds(dataSec) : '--',
    });
    setMetricsVisible(true);
  }
  try {
    const res = await offlineSession(sessionId);
    const sess = res && res.session ? res.session : null;
    const d = res && res.derived ? res.derived : null;
    if (sess && sess.session_id) cacheSessionMeta(sess);
    const samples = sess && typeof sess.total_samples === 'number' ? sess.total_samples : null;
    const dataChannels = d && typeof d.channels === 'number' ? d.channels : (sess && Array.isArray(sess.channel_names) ? sess.channel_names.length : null);
    const eegChannels = getEegChannelCount(sess, dataChannels);
    const dataSec = d && typeof d.data_duration_sec === 'number' ? d.data_duration_sec : null;
    const estMiB = (samples && dataChannels) ? ((samples * dataChannels * 4) / (1024.0 * 1024.0)) : null;
    setMetricsData({
      samples: `${fmtInt(samples)} 点`,
      channels: `${fmtInt(eegChannels)} 通道`,
      size: `${estMiB !== null ? fmtMiB(estMiB) : '--'} MiB`,
      duration: dataSec !== null ? fmtSeconds(dataSec) : '--',
    });
    setMetricsVisible(true);
    setSessionInfo({ session_id: sess && sess.session_id ? sess.session_id : sessionId, session_dir: sess && sess.session_dir ? sess.session_dir : getLastSessionDir() });
  } catch (_) {
    if (!cached) {
      setMetricsMessage('数据概况获取失败，可继续导出，导出结果不受影响。');
      setMetricsVisible(true);
    }
  }
}

export async function enterOfflinePage() {
  pageActive = true;
  lastSessionId = getLastSessionId();
  const lastSessionDir = getLastSessionDir();
  setStatus(lastSessionId ? '数据已就绪，请选择导出选项。' : '未检测到最近会话，请先进入 EEG 页面开始/停止一次采集。', lastSessionId ? 'success' : 'error');
  setSessionInfo(null);
  setMetricsVisible(false);

  const backBtn = document.getElementById('btn-offline-back');
  const exportBtn = document.getElementById('btn-offline-export');
  const openFolderBtn = document.getElementById('btn-offline-open-folder');
  const filterToggle = document.getElementById('offline-filter-enable');
  const baseRaw = document.getElementById('offline-base-raw');
  const baseFil = document.getElementById('offline-base-filtered');

  setOpenFolderButtonVisible(!!lastSessionId);

  if (baseRaw && !baseRaw.value) baseRaw.value = 'eeg';
  if (baseFil && !baseFil.value) baseFil.value = 'eeg_filtered';

  if (filterToggle) {
    filterToggle.onchange = () => {
      setFilterVisible(!!filterToggle.checked);
    };
    setFilterVisible(!!filterToggle.checked);
  }

  if (backBtn) backBtn.onclick = async () => { await navigate('#mode'); };

  if (openFolderBtn) {
    openFolderBtn.onclick = async () => {
      if (!lastSessionId) return;
      openFolderBtn.disabled = true;
      setStatus('正在打开会话目录…', '');
      try {
        await offlineOpenFolder(lastSessionId);
        setStatus('已打开会话目录', 'success');
      } catch (e) {
        setStatus(`打开文件夹失败：${e && e.message ? e.message : '未知错误'}`, 'error');
      } finally {
        setOpenFolderButtonVisible(!!lastSessionId);
      }
    };
  }

  if (exportBtn) {
    exportBtn.disabled = !lastSessionId;
    exportBtn.onclick = async () => {
      if (!lastSessionId) return;
      const targets = buildTargets();
      if (!targets.length) {
        setStatus('请至少选择一种导出文件（CSV/EDF 或滤波后文件）。', 'error');
        return;
      }

      const filterEnabled = !!document.getElementById('offline-filter-enable')?.checked;
      const wantFiltered = targets.some(t => t.kind === 'filtered');
      if (wantFiltered && !filterEnabled) {
        setStatus('已选择“滤波后文件”，请先勾选“启用带通滤波”。', 'error');
        return;
      }

      const baseNameRaw = String(document.getElementById('offline-base-raw')?.value || 'eeg').trim();
      const baseNameFiltered = String(document.getElementById('offline-base-filtered')?.value || 'eeg_filtered').trim();

      const lowcut = readNumber('offline-lowcut', 3.0);
      const highcut = readNumber('offline-highcut', 50.0);
      if (filterEnabled) {
        if (!(lowcut > 0) || !(highcut > 0) || !(highcut > lowcut)) {
          setStatus('滤波参数非法：需要满足 0 < 低频截止 < 高频截止。', 'error');
          return;
        }
      }

      exportBtn.disabled = true;
      setStatus('导出中，请稍候…', '');
      try {
        const payload = {
          session_id: lastSessionId,
          base_name_raw: baseNameRaw,
          base_name_filtered: baseNameFiltered,
          targets,
          bandpass: { enabled: filterEnabled, lowcut_hz: lowcut, highcut_hz: highcut },
        };
        const res = await offlineExport(payload);
        const outputs = res && res.result && Array.isArray(res.result.outputs) ? res.result.outputs : [];
        if (!outputs.length) {
          setStatus('未生成任何文件（请检查导出选项）。', 'error');
          return;
        }
        setStatus('导出成功', 'success');
      } catch (e) {
        setStatus(`导出失败：${e && e.message ? e.message : '未知错误'}`, 'error');
      } finally {
        exportBtn.disabled = false;
      }
    };
  }

  try {
    const cfg = await getConfig();
    setDefaultsFromConfig(cfg);
  } catch (_) {}

  if (!lastSessionId) return;
  try {
    setSessionInfo({ session_id: lastSessionId, session_dir: lastSessionDir });
  } catch (_) {}
  await loadAndRenderMetrics(lastSessionId);
}

export async function leaveOfflinePage() {
  pageActive = false;
}
