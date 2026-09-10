/* Music playback is browser-owned; EEG acquisition, units and saving stay in EEGSuite. */
import { getStatus, musicLibrary, musicLyrics, musicResults, musicStart, musicHeartbeat, musicTrialStart, musicEvent, musicTrialStop, musicStop, musicExport, musicOpenExportFolder } from './api.js';
import { navigate } from '/web/js/router.js';

const $ = id => document.getElementById(id);
const TOKEN_KEY = 'bhb_music_token';
let songs = [], selected = new Set(), run = null, bound = false, pageActive = false;
let deviceReady = false, deviceBusy = false, libraryLoading = false;
let results = [];
let debugLines = [], debugBufferedLines = [], debugFilterText = '', debugPaused = false;
let debugWs = null, debugReconnectTimer = null, debugFocus = false;
const MUSIC_DEBUG_FOCUS_KEY = 'bhb_music_debug_focus';
const reasonLabels = { completed: '实验完成', stopped: '实验已停止', escape: '实验已中止', page_left: '离开页面，实验已停止', playback_error: '播放异常，实验已停止', request_error: '连接异常，实验已停止', device_lost: '设备断开，实验已停止', browser_lost: '浏览器连接中断，实验已停止', eeg_timeout: '未收到 EEG 数据，实验已停止' };
const message = error => String(error?.message || error || '未知错误');
const clock = sec => `${String(Math.floor(Math.max(0, sec || 0) / 60)).padStart(2, '0')}:${String(Math.floor(Math.max(0, sec || 0) % 60)).padStart(2, '0')}`;
function status(text, kind = '') {
  const el = $('music-status');
  if (!el) return;
  el.textContent = text;
  el.className = `status-box ${kind}`;
}
function renderDebug() {
  const el = $('music-log');
  if (!el) return;
  const stick = el.scrollTop + el.clientHeight >= el.scrollHeight - 6;
  const filter = debugFilterText.trim().toLowerCase();
  const lines = filter ? debugLines.filter(line => line.toLowerCase().includes(filter)) : debugLines;
  el.textContent = lines.join('\n');
  if (!debugPaused && stick) el.scrollTop = el.scrollHeight;
}
function appendDebug(line) {
  if (debugPaused) {
    debugBufferedLines.push(line);
    if (debugBufferedLines.length > 2000) debugBufferedLines = debugBufferedLines.slice(-2000);
    return;
  }
  debugLines.push(line);
  if (debugLines.length > 2000) debugLines = debugLines.slice(-2000);
  renderDebug();
}
function log(text) {
  const now = new Date();
  const stamp = now.toLocaleString('zh-CN', { hour12: false }) + `.${String(now.getMilliseconds()).padStart(3, '0')}`;
  appendDebug(`${stamp}\t[MUSIC      ]\t${text}`);
}
function formatDebugEvent(event) {
  const timestamp = new Date((Number(event?.ts) || Date.now() / 1000) * 1000);
  const pad = value => String(value).padStart(2, '0');
  const stamp = `${timestamp.getFullYear()}-${pad(timestamp.getMonth() + 1)}-${pad(timestamp.getDate())} ${pad(timestamp.getHours())}:${pad(timestamp.getMinutes())}:${pad(timestamp.getSeconds())}.${String(timestamp.getMilliseconds()).padStart(3, '0')}`;
  const tag = String(event?.tag || 'DEBUG').slice(0, 12).padEnd(12, ' ');
  let data = '';
  try { if (event?.data && Object.keys(event.data).length) data = JSON.stringify(event.data); } catch (_) {}
  return `${stamp}\t[${tag}]\t${event?.message || ''}\t${data}`;
}
function connectDebug() {
  if (!pageActive || debugWs) return;
  const protocol = location.protocol === 'https:' ? 'wss' : 'ws';
  debugWs = new WebSocket(`${protocol}://${location.host}/ws/debug`);
  debugWs.onmessage = event => {
    try {
      const payload = JSON.parse(event.data);
      if (payload.type === 'debug_init' && Array.isArray(payload.events)) {
        debugLines = payload.events.map(formatDebugEvent).slice(-2000);
        if (!debugPaused) renderDebug();
      } else if (payload.type === 'debug_event' && payload.event) {
        appendDebug(formatDebugEvent(payload.event));
      }
    } catch (_) {}
  };
  debugWs.onclose = () => {
    debugWs = null;
    if (!pageActive || debugReconnectTimer) return;
    debugReconnectTimer = setTimeout(() => { debugReconnectTimer = null; connectDebug(); }, 1000);
  };
}
function closeDebug() {
  if (debugReconnectTimer) clearTimeout(debugReconnectTimer);
  debugReconnectTimer = null;
  if (debugWs) { const socket = debugWs; debugWs = null; socket.onclose = null; socket.close(); }
}
function setDebugFocus(enabled, persist = true) {
  debugFocus = !!enabled;
  const layout = document.querySelector('#page-music .music-layout');
  const body = $('music-debug-body');
  const toggle = $('music-focus-toggle');
  const label = $('music-focus-label');
  if (body) body.hidden = false;
  if (layout) layout.classList.toggle('music-layout--debug-focus', debugFocus);
  if (body) { body.setAttribute('aria-hidden', String(debugFocus)); body.inert = debugFocus; }
  const nextLabel = debugFocus ? '显示调试' : '隐藏调试';
  if (toggle) {
    toggle.classList.toggle('is-active', debugFocus);
    toggle.setAttribute('aria-pressed', String(debugFocus));
    toggle.setAttribute('aria-expanded', String(!debugFocus));
    toggle.setAttribute('aria-label', nextLabel);
    toggle.title = nextLabel;
  }
  if (label) label.textContent = nextLabel;
  if (persist) { try { localStorage.setItem(MUSIC_DEBUG_FOCUS_KEY, debugFocus ? '1' : '0'); } catch (_) {} }
}
function timing(r) {
  return { client_time_unix_ms: Date.now(), client_monotonic_ms: performance.now(), audio_time_sec: Number.isFinite(r.audio.currentTime) ? r.audio.currentTime : 0 };
}
function ensureRunning(r) { if (run !== r || r.cancelled) throw new DOMException('实验已中止', 'AbortError'); }
function setControls() {
  const busy = !!run;
  $('music-start').disabled = busy || !deviceReady || deviceBusy || !selected.size || libraryLoading;
  $('music-stop').disabled = !busy || !!run?.stopPromise;
  for (const id of ['music-all', 'music-none', 'music-refresh', 'music-subject', 'music-volume', 'music-fullscreen']) $(id).disabled = busy || libraryLoading;
  document.querySelectorAll('.music-song').forEach(el => { el.disabled = busy; });
  $('music-selection').textContent = `已选 ${selected.size} / ${songs.length} 首`;
  if ($('music-saved-count')) $('music-saved-count').textContent = `已保存 ${results.length} 首`;
  $('music-export').disabled = busy || !results.length;
  window.dispatchEvent(new CustomEvent('app:music-lock', { detail: busy }));
}
function applyStatus(snapshot) {
  const dev = snapshot?.device;
  deviceReady = !!dev?.running && ['connected', 'ready'].includes(dev?.last?.type);
  deviceBusy = !!dev?.task_running || !!snapshot?.music?.active || !!snapshot?.lsl_streaming;
  setControls();
  if (!run && pageActive) {
    if (!deviceReady) status('请先在设备页连接设备并应用通道配置。');
    else if (deviceBusy) status('设备正在运行其他任务，请先停止再开始文本-音频多模态实验。');
  }
}
function renderSongs() {
  const root = $('music-songs'); root.replaceChildren();
  if (!songs.length) {
    const empty = document.createElement('div'); empty.className = 'music-empty';
    empty.textContent = '暂无可用音频，请检查模块资源后刷新曲库。'; root.append(empty);
  }
  for (const song of songs) {
    const btn = document.createElement('button'); btn.type = 'button'; btn.className = 'music-song';
    btn.setAttribute('aria-pressed', String(selected.has(song.id)));
    btn.innerHTML = '<span class="music-song-number"></span><strong class="music-song-title"></strong><span class="music-song-check" aria-hidden="true"></span>';
    btn.querySelector('.music-song-number').textContent = String(song.category).padStart(2, '0');
    btn.querySelector('.music-song-title').textContent = song.name;
    btn.querySelector('.music-song-check').textContent = selected.has(song.id) ? '✓' : '○';
    btn.onclick = () => { if (run) return; selected.has(song.id) ? selected.delete(song.id) : selected.add(song.id); renderSongs(); };
    root.append(btn);
  }
  setControls();
}
async function loadLibrary() {
  if (run || libraryLoading) return;
  libraryLoading = true; setControls();
  try {
    const data = await musicLibrary();
    const known = new Set(songs.map(s => s.id));
    songs = data.songs || [];
    selected = new Set(songs.filter(s => !known.has(s.id) || selected.has(s.id)).map(s => s.id));
    renderSongs();
    status(`已加载 ${songs.length} 首歌曲，按曲库编号顺序执行。`, 'success');
    applyStatus(await getStatus());
  } catch (error) { status(`曲库加载失败：${message(error)}`, 'error'); }
  finally { libraryLoading = false; setControls(); }
}
function cacheSession(session) {
  if (!session?.session_id) return;
  try {
    sessionStorage.setItem('bhb_last_eeg_session', session.session_id);
    sessionStorage.setItem('bhb_last_eeg_session_dir', session.session_dir);
    sessionStorage.setItem('bhb_last_eeg_session_meta', JSON.stringify(session));
  } catch (_) {}
}
function persistResults() {
  try { sessionStorage.setItem('bhb_music_results', JSON.stringify(results)); } catch (_) {}
}
function restoreResults() {
  try {
    const saved = JSON.parse(sessionStorage.getItem('bhb_music_results') || '[]');
    if (Array.isArray(saved)) results = saved.filter(item => item?.session?.session_id && item?.song?.name);
  } catch (_) { results = []; }
}
function addResult(result) {
  if (!result?.session?.session_id) return;
  const existing = results.findIndex(r => r.session.session_id === result.session.session_id);
  if (existing >= 0) results[existing] = result; else results.push(result);
  cacheSession(result.session);
  persistResults();
  const count = $('music-saved-count');
  if (count) count.textContent = `已保存 ${results.length} 首`;
  const exportButton = $('music-export');
  if (exportButton) exportButton.disabled = !!run || !results.length;
}
function renderMetrics(snap) {
  $('music-progress').textContent = `${snap.completed || 0} / ${snap.total || 0}`;
  $('music-samples').textContent = String(snap.samples || 0);
  $('music-rate').textContent = snap.effective_rate_hz > 0 ? `${snap.effective_rate_hz.toFixed(1)} Hz` : '-- Hz';
  $('music-elapsed').textContent = clock(snap.elapsed_sec);
}
async function heartbeat(r) {
  if (run !== r || r.cancelled) return;
  try {
    const snap = await musicHeartbeat(r.token);
    if (run !== r || r.cancelled) return;
    r.ready = snap.ready;
    renderMetrics(snap);
  } catch (error) {
    if (!r.cancelled) { log(`采集状态异常：${message(error)}`); void stopExperiment('request_error'); }
    return;
  }
  if (!r.cancelled) r.heartbeatTimer = setTimeout(() => heartbeat(r), 1000);
}
function delay(r, ms) {
  return new Promise((resolve, reject) => {
    const cancelled = () => { clearTimeout(timer); reject(new DOMException('实验已中止', 'AbortError')); };
    const timer = setTimeout(() => { r.abort.signal.removeEventListener('abort', cancelled); resolve(); }, ms);
    r.abort.signal.addEventListener('abort', cancelled, {once: true});
    if (r.cancelled) cancelled();
  });
}
async function readyAudio(r, song) {
  r.audio.src = `/api/music/audio/${encodeURIComponent(song.id)}`;
  r.audio.load();
  await new Promise((resolve, reject) => {
    const cleanup = () => { clearTimeout(timer); r.audio.removeEventListener('canplaythrough', loaded); r.audio.removeEventListener('error', failed); r.abort.signal.removeEventListener('abort', aborted); };
    const loaded = () => { cleanup(); resolve(); };
    const failed = () => { cleanup(); reject(new Error('音频无法解码或读取，请检查文件格式')); };
    const aborted = () => { cleanup(); reject(new DOMException('实验已中止', 'AbortError')); };
    const timer = setTimeout(() => { cleanup(); reject(new Error('音频加载超时')); }, 15000);
    r.audio.addEventListener('canplaythrough', loaded, {once: true});
    r.audio.addEventListener('error', failed, {once: true});
    r.abort.signal.addEventListener('abort', aborted, {once: true});
    if (r.audio.readyState >= 4) loaded();
    if (r.cancelled) aborted();
  });
}
function renderLyrics(data, song) {
  const root = $('music-lyrics'); root.replaceChildren(); root.scrollTop = 0;
  if (data.cues?.length) {
    for (const cue of data.cues) { const line = document.createElement('div'); line.className = 'music-lyric-line'; line.textContent = cue.text || '♪'; root.append(line); }
  } else root.textContent = data.text || `${song.name}\n暂无歌词，请聆听音乐`;
}
function animateLyrics(r, data) {
  let last = -2;
  r.visualTimer = setInterval(() => {
    if (r.cancelled) return;
    $('music-stage-time').textContent = `${clock(r.audio.currentTime)} / ${clock(r.audio.duration)}`;
    let idx = -1;
    (data.cues || []).forEach((cue, i) => { if (cue.time <= r.audio.currentTime) idx = i; });
    if (idx === last) return;
    last = idx;
    [...$('music-lyrics').children].forEach((line, i) => line.classList.toggle('is-current', i === idx));
    if (idx >= 0) $('music-lyrics').children[idx]?.scrollIntoView({block: 'center', behavior: 'smooth'});
  }, 100);
}
function playSong(r, data) {
  return new Promise((resolve, reject) => {
    let events = Promise.resolve(), bufferTimer = null;
    const cleanup = () => { clearTimeout(bufferTimer); clearInterval(r.visualTimer); r.audio.onplaying = r.audio.onended = r.audio.onerror = r.audio.onwaiting = r.audio.onpause = null; r.abort.signal.removeEventListener('abort', aborted); };
    const fail = (error) => { cleanup(); reject(error); };
    const aborted = () => fail(new DOMException('实验已中止', 'AbortError'));
    const event = kind => { const observed = timing(r); events = events.then(() => musicEvent(r.token, r.index, kind, observed)); events.catch(fail); };
    r.audio.onplaying = () => { clearTimeout(bufferTimer); event('playing'); $('music-stage-state').textContent = '正在播放 · EEG 采集中'; };
    r.audio.onwaiting = () => { event('waiting'); $('music-stage-state').textContent = '音频缓冲中'; bufferTimer = setTimeout(() => fail(new Error('音频持续缓冲，实验已中止')), 5000); };
    r.audio.onerror = () => fail(new Error('音频播放失败'));
    r.audio.onpause = () => { if (!r.cancelled && !r.audio.ended) fail(new Error('音频播放被中断')); };
    r.audio.onended = async () => { const observed = timing(r); cleanup(); try { await events; resolve(observed); } catch (error) { reject(error); } };
    r.abort.signal.addEventListener('abort', aborted, {once: true});
    animateLyrics(r, data);
    r.audio.play().catch(error => fail(new Error(`浏览器无法播放音频：${message(error)}`)));
  });
}
async function executePlaylist(r) {
  for (r.index = 0; r.index < r.playlist.length; r.index++) {
    ensureRunning(r);
    const song = r.playlist[r.index];
    $('music-now').textContent = song.name;
    $('music-stage-song').textContent = song.name;
    $('music-stage-position').textContent = `${r.index + 1} / ${r.playlist.length} · 文本-音频多模态范式`;
    $('music-stage-state').textContent = '正在准备音频与 EEG 数据';
    $('music-countdown').textContent = '';
    $('music-lyrics').textContent = '正在准备…';
    const [lyrics] = await Promise.all([musicLyrics(song.id), readyAudio(r, song)]);
    ensureRunning(r);
    const deadline = performance.now() + 20000;
    while (!r.ready) { await delay(r, 200); ensureRunning(r); if (performance.now() > deadline) throw new Error('等待 EEG 数据超时'); }
    $('music-lyrics').textContent = '请保持静止，准备聆听';
    const countdownEnd = performance.now() + 3000;
    while (performance.now() < countdownEnd) {
      $('music-countdown').textContent = String(Math.max(1, Math.ceil((countdownEnd - performance.now()) / 1000)));
      await delay(r, Math.min(200, countdownEnd - performance.now()));
      ensureRunning(r);
    }
    await musicTrialStart(r.token, r.index);
    ensureRunning(r);
    $('music-countdown').textContent = '';
    renderLyrics(lyrics, song);
    log(`开始播放与录制：${song.name}`);
    const observed = await playSong(r, lyrics);
    ensureRunning(r);
    $('music-stage-state').textContent = '正在保存本首实验数据…';
    const result = await musicTrialStop(r.token, r.index, observed);
    addResult(result);
    log(`${song.name}：${result.session.total_samples} 点，实际接收 ${result.effective_rate_hz.toFixed(1)} Hz`);
    if (result.errors?.length) throw new Error(result.errors.join('；'));
    ensureRunning(r);
  }
  await stopExperiment('completed');
}
async function startExperiment() {
  if (run || !deviceReady || deviceBusy || !selected.size) return;
  const r = {token: '', playlist: songs.filter(s => selected.has(s.id)), index: 0, audio: new Audio(), abort: new AbortController(), ready: false, cancelled: false, stopPromise: null};
  r.audio.preload = 'auto'; r.audio.volume = Number($('music-volume').value) / 100;
  run = r; results = []; persistResults(); setControls();
  const stage = $('music-stage'); stage.hidden = false;
  $('music-stage-stop').focus();
  $('music-lyrics').textContent = '正在启动采集…';
  $('music-countdown').textContent = '';
  if ($('music-fullscreen').checked && stage.requestFullscreen) {
    // Request directly in the user's click, before any network await.
    stage.requestFullscreen().catch(() => log('浏览器未进入全屏，继续使用沉浸式歌词面板'));
  }
  status('实验正在运行；点击停止并导出可中止。', 'success');
  r.startRequest = musicStart({song_ids: r.playlist.map(s => s.id), subject: $('music-subject').value.trim()});
  try {
    const response = await r.startRequest;
    r.token = response.token;
    try { sessionStorage.setItem(TOKEN_KEY, r.token); } catch (_) {}
    ensureRunning(r);
    log(`实验开始：${r.playlist.length} 首歌曲`);
    void heartbeat(r);
    await executePlaylist(r);
  } catch (error) {
    if (error.name === 'AbortError' || r.cancelled) return;
    log(message(error));
    r.error = message(error);
    await stopExperiment('playback_error');
  }
}
async function stopExperiment(reason = 'stopped') {
  const r = run;
  if (!r) return;
  if (r.stopPromise) return r.stopPromise;
  r.cancelled = true;
  const observed = timing(r);
  r.abort.abort(); r.audio.pause();
  clearTimeout(r.heartbeatTimer); clearInterval(r.visualTimer);
  r.stopPromise = (async () => {
    let finalReason = reason, errors = [];
    try {
      // Stopping during startup must wait for ownership before sending stop.
      if (!r.token) { const started = await r.startRequest; r.token = started.token; }
      const response = await musicStop(r.token, reason, observed);
      finalReason = response.reason || reason;
      (response.results || []).forEach(addResult);
      errors = [...(response.errors || []), ...(response.results || []).flatMap(x => x.errors || [])];
      try { sessionStorage.removeItem(TOKEN_KEY); } catch (_) {}
    } catch (error) {
      errors.push(`停止确认失败：${message(error)}；后端将根据心跳超时收尾，请检查已保存会话。`);
    } finally {
      $('music-stage').hidden = true;
      try { if (document.fullscreenElement === $('music-stage')) await document.exitFullscreen(); } catch (_) {}
      r.audio.removeAttribute('src'); r.audio.load();
      run = null;
      const label = reasonLabels[finalReason] || '实验已停止';
      const detail = [r.error, ...errors].filter(Boolean).join('；');
      status(detail ? `${label}。${detail}` : `${label}${results.length ? '，数据已保存。' : '，本次尚未产生歌曲数据。'}`, detail ? 'error' : (finalReason === 'completed' ? 'success' : ''));
      log(detail || label);
      if (results.length) { $('music-progress').textContent = `${results.length} / ${r.playlist.length}`; addResult(results[results.length - 1]); }
      deviceBusy = false;
      setControls();
      if (['completed', 'stopped'].includes(finalReason)) {
        await navigate('#music-export');
      } else if (pageActive) {
        $('music-start').focus();
      }
      getStatus().then(applyStatus).catch(() => {});
    }
  })();
  setControls();
  return r.stopPromise;
}
function bind() {
  if (bound) return; bound = true;
  $('music-start').onclick = startExperiment;
  $('music-stop').onclick = () => stopExperiment();
  $('music-stage-stop').onclick = () => stopExperiment();
  $('music-all').onclick = () => { if (!run) { selected = new Set(songs.map(s => s.id)); renderSongs(); } };
  $('music-none').onclick = () => { if (!run) { selected.clear(); renderSongs(); } };
  $('music-refresh').onclick = loadLibrary;
  $('music-debug-clear').onclick = () => { debugLines = []; debugBufferedLines = []; renderDebug(); };
  $('music-debug-pause').onclick = () => {
    debugPaused = !debugPaused;
    $('music-debug-pause').textContent = debugPaused ? '继续滚动' : '暂停输出';
    if (!debugPaused) {
      debugLines = debugLines.concat(debugBufferedLines).slice(-2000);
      debugBufferedLines = [];
      renderDebug();
    }
  };
  $('music-debug-filter').oninput = event => { debugFilterText = event.target.value || ''; renderDebug(); };
  $('music-focus-toggle').onclick = () => setDebugFocus(!debugFocus);
  $('music-volume').oninput = () => { $('music-volume-value').textContent = `${$('music-volume').value}%`; };
  $('music-export').onclick = () => { if (results.length && !run) navigate('#music-export'); };
  window.addEventListener('app:status', e => applyStatus(e.detail));
  document.addEventListener('keydown', e => {
    if (!run) return;
    if (e.key === 'Escape') { e.preventDefault(); void stopExperiment('escape'); }
    if (e.key === 'Tab') { e.preventDefault(); $('music-stage-stop').focus(); }
  });
  document.addEventListener('fullscreenchange', () => { if (run && !run.cancelled && !document.fullscreenElement) void stopExperiment('escape'); });
  window.addEventListener('pagehide', () => {
    if (!run?.token) return;
    run.audio.pause();
    navigator.sendBeacon('/api/music/stop', new Blob([JSON.stringify({token: run.token, reason: 'page_left', ...timing(run)})], {type: 'application/json'}));
  });
}
export async function enterMusicPage() {
  pageActive = true; bind();
  restoreResults();
  if ($('music-saved-count')) $('music-saved-count').textContent = `已保存 ${results.length} 首`;
  try { setDebugFocus(localStorage.getItem(MUSIC_DEBUG_FOCUS_KEY) === '1', false); } catch (_) { setDebugFocus(false, false); }
  $('music-debug-pause').textContent = debugPaused ? '继续滚动' : '暂停输出';
  connectDebug();
  if (run) return;
  let previous = '';
  try { previous = sessionStorage.getItem(TOKEN_KEY) || ''; } catch (_) {}
  if (previous) {
    try {
      const saved = await musicStop(previous, 'page_left');
      (saved.results || []).forEach(addResult);
      sessionStorage.removeItem(TOKEN_KEY);
      log('已收尾刷新前的实验，并恢复保存记录。');
    } catch (error) { log(`先前实验收尾状态：${message(error)}`); }
  }
  await loadLibrary();
}
export async function leaveMusicPage() { pageActive = false; closeDebug(); await stopExperiment('page_left'); }

let exportPageActive = false;
let exportPageBound = false;
let exportResults = [];
const MUSIC_EXPORT_KEY = 'bhb_music_results';

function exportStatus(text, kind = '') {
  const box = $('music-export-status');
  if (!box) return;
  const value = String(text || '').trim();
  box.textContent = value.startsWith('状态：') ? value : `状态：${value || '等待操作'}`;
  box.className = `status-box ${kind || 'error'}`;
}

function setExportDir(path, visible = Boolean(path)) {
  const dir = $('music-export-dir');
  if (dir) {
    dir.textContent = path || '--';
    dir.title = path || '';
  }
  const open = $('music-export-open-folder');
  if (open) {
    open.style.display = visible ? '' : 'none';
    open.disabled = !visible;
  }
}

function selectedExportFormats() {
  const formats = [];
  if ($('music-export-csv')?.checked) formats.push('csv');
  if ($('music-export-edf')?.checked) formats.push('edf');
  return formats;
}

function renderExportFormat() {
  const formats = selectedExportFormats();
  const metric = $('music-export-format');
  if (metric) metric.textContent = formats.length ? formats.map(format => format.toUpperCase()).join(' + ') : '未选择';
}

function renderExportResults() {
  const root = $('music-export-list');
  if (!root) return;
  root.replaceChildren();
  const count = $('music-export-count');
  if (count) count.textContent = `${exportResults.length} 个`;
  if (!exportResults.length) {
    const empty = document.createElement('div');
    empty.className = 'music-export-empty-note offline-filter-idle';
    empty.textContent = '暂无已保存的文本-音频会话。完成一次实验后，数据会自动显示在这里。';
    root.append(empty);
    return;
  }
  exportResults.forEach((result, index) => {
    const item = document.createElement('article');
    item.className = 'music-export-item';
    const main = document.createElement('div'); main.className = 'music-export-item-main';
    const title = document.createElement('strong'); title.textContent = `${index + 1}. ${result.song.name}`;
    const detail = document.createElement('div'); detail.className = 'music-muted';
    detail.textContent = `${result.session.total_samples || 0} 点 · ${result.session.channel_names?.length || '--'} 通道 · ${result.session.session_id}`;
    const path = document.createElement('div'); path.className = 'music-export-item-path'; path.textContent = result.session.session_dir || '--'; path.title = result.session.session_dir || '';
    main.append(title, detail, path);
    const badge = document.createElement('span'); badge.className = 'music-export-item-index'; badge.textContent = String(index + 1).padStart(2, '0');
    item.append(badge, main);
    root.append(item);
  });
}

async function exportAll() {
  const button = $('music-export-all-files');
  if (!exportResults.length) {
    exportStatus('暂无可导出的文本-音频实验会话', 'error');
    return;
  }
  const formats = selectedExportFormats();
  if (!formats.length) {
    exportStatus('请至少选择一种导出文件格式', 'error');
    return;
  }
  if (button) button.disabled = true;
  exportStatus(`正在导出 ${exportResults.length} 个项目的 ${formats.map(format => format.toUpperCase()).join(' 和 ')}…`, '');
  try {
    const response = await musicExport(formats, exportResults);
    const outputs = Array.isArray(response?.outputs) ? response.outputs : [];
    const errors = Array.isArray(response?.errors) ? response.errors : [];
    const missing = Array.isArray(response?.missing_indexes) ? response.missing_indexes : [];
    setExportDir(response?.export_dir || '', Boolean(response?.export_dir));
    const files = $('music-export-files');
    if (files) files.textContent = `${outputs.length} 个`;
    const done = exportResults.length - errors.length;
    let text = `导出完成：${Math.max(0, done)} 个项目，${outputs.length} 个文件`;
    if (missing.length) text += `，空置编号 ${missing.join('、')}`;
    if (errors.length) text += `，失败 ${errors.length} 个`;
    exportStatus(text, errors.length ? 'error' : 'success');
  } catch (error) {
    exportStatus(`导出失败：${message(error)}`, 'error');
  } finally {
    if (button) button.disabled = false;
  }
}

async function loadExportResults() {
  let local = [];
  try { local = JSON.parse(sessionStorage.getItem(MUSIC_EXPORT_KEY) || '[]'); } catch (_) {}
  try {
    const remote = await musicResults();
    if (Array.isArray(remote?.results) && remote.results.length) local = remote.results;
  } catch (_) {}
  const unique = new Map();
  for (const result of local) if (result?.session?.session_id && result?.song?.name) unique.set(result.session.session_id, result);
  exportResults = [...unique.values()];
  renderExportResults();
  const button = $('music-export-all-files');
  if (button) button.disabled = exportResults.length === 0;
  if (!exportResults.length) setExportDir('', false);
  exportStatus(exportResults.length ? `已读取 ${exportResults.length} 个已完成项目，默认全部导出` : '暂无已保存的文本-音频会话', exportResults.length ? 'success' : 'error');
}

function bindExportPage() {
  if (exportPageBound) return;
  exportPageBound = true;
  $('music-export-back').onclick = () => navigate('#music');
  $('music-export-mode').onclick = () => navigate('#mode');
  $('music-export-all-files').onclick = exportAll;
  $('music-export-csv').onchange = renderExportFormat;
  $('music-export-edf').onchange = renderExportFormat;
  $('music-export-open-folder').onclick = async () => {
    const button = $('music-export-open-folder');
    button.disabled = true;
    try {
      await musicOpenExportFolder();
      exportStatus('已打开共同导出目录', 'success');
    } catch (error) {
      exportStatus(`打开文件夹失败：${message(error)}`, 'error');
    } finally { button.disabled = false; }
  };
}

export async function enterMusicExportPage() {
  exportPageActive = true;
  bindExportPage();
  renderExportFormat();
  await loadExportResults();
}

export async function leaveMusicExportPage() { exportPageActive = false; }
