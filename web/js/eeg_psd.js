/*
Copyright (c) 2026 BUAA BHB. All rights reserved.

文件功能: EEG 频谱与频带特征视图：展示实时 Welch PSD、频带能量、微分熵及多/单通道切换
作者: Spoon
*/

import {
  getSignalQuality,
  getSignalQualitySnapshot,
  setSignalQuality,
  setSignalQualityManualChannels,
  triggerStart,
  triggerStop,
} from './api.js';
import { createSelectableTopomap } from './impedance_topomap.js';

const PSD_DISPLAY_MIN_HZ = 1;
const PSD_DISPLAY_MAX_HZ = 45;
const VARIANCE_TREND_WINDOW_MS = 30000;
const VARIANCE_YAXIS_DYNAMIC_KEY = 'bhb_eeg_variance_yaxis_dynamic';
const VARIANCE_YAXIS_FIXED_MAX_KEY = 'bhb_eeg_variance_yaxis_fixed_max_raw';
const VARIANCE_YAXIS_EXPONENT_MIN = -12;
const VARIANCE_YAXIS_EXPONENT_MAX = 12;
const VARIANCE_YAXIS_EXPONENT_STEP = 0.1;
const VARIANCE_YAXIS_FIXED_MAX_DEFAULT = 1e5;

const MAP_HINT_DEFAULT = '点选电极分析单一通道，点选2个进入对比视图。';
const MAP_HINT_FULL_CHANNEL = '全通道分析已开启，图表显示全通道平均结果。';
const MAP_HINT_WARN = '最多对比 2 个通道，请先取消一个';

const GEAR_SVG = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>';
const CLOSE_SVG = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>';

const FALLBACK_BANDS = [
  { key: 'delta', name: 'Delta', symbol: '', fmin_hz: 1, fmax_hz: 4 },
  { key: 'theta', name: 'Theta', symbol: '', fmin_hz: 4, fmax_hz: 8 },
  { key: 'alpha', name: 'Alpha', symbol: '', fmin_hz: 8, fmax_hz: 13 },
  { key: 'beta', name: 'Beta', symbol: '', fmin_hz: 13, fmax_hz: 30 },
  { key: 'gamma', name: 'Gamma', symbol: '', fmin_hz: 30, fmax_hz: 45 },
];

// 与 attention_monitor.py 保持一致，图例、频谱底色和柱状图共用这组颜色。
const BAND_COLORS = {
  delta: '#8DA0CB',
  theta: '#66C2A5',
  alpha: '#FFD166',
  beta: '#06D6A0',
  gamma: '#EF476F',
};

function escapeHtml(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}

function formatHz(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return '--';
  return Number.isInteger(n) ? String(n) : String(Number(n.toFixed(1)));
}

function formatPower(value) {
  const n = Number(value);
  if (!Number.isFinite(n) || n < 0) return '--';
  if (n >= 10000) return n.toExponential(1);
  if (n >= 100) return n.toFixed(1);
  if (n >= 1) return n.toFixed(2);
  return n.toFixed(3);
}

function formatScientific(value, precision = 1) {
  const n = Number(value);
  if (!Number.isFinite(n)) return '--';
  return n.toExponential(precision);
}

function hexToRgba(hex, alpha) {
  const raw = String(hex || '').replace('#', '');
  if (!/^[0-9a-fA-F]{6}$/.test(raw)) return `rgba(56, 189, 248, ${alpha})`;
  const r = Number.parseInt(raw.slice(0, 2), 16);
  const g = Number.parseInt(raw.slice(2, 4), 16);
  const b = Number.parseInt(raw.slice(4, 6), 16);
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

export class EegPsdView {
  constructor({
    channelNames,
    triggerEnabled = false,
    triggerActive = null,
    electrodePositions = null,
    electrodeAliases = null,
  }) {
    this.channelNames = Array.isArray(channelNames) ? channelNames.map(String) : [];
    this.mode = 'time';
    // Keep the original default: the frequency view starts with all-channel analysis.
    this.scopeMode = 'average';
    this.bandMetricMode = 'energy';
    this.varianceYAxisDynamicEnabled = true;
    this.varianceYAxisFixedMax = VARIANCE_YAXIS_FIXED_MAX_DEFAULT;
    this.activeChannels = this.channelNames.length ? [this.channelNames[0]] : [];
    this.psdWs = null;
    this.varianceWs = null;
    this.psdReconnectTimer = null;
    this.varianceReconnectTimer = null;
    this.psdReconnectAttempt = 0;
    this.varianceReconnectAttempt = 0;
    this.disposed = false;
    this.psdPayload = null;
    this.variancePayload = null;
    this.varianceHistory = [];
    this.channelQuality = null;
    this.qualityPolicy = null;
    this.manualQualityMode = false;
    this.qualityBusy = false;
    this.qualityError = '';
    this.theme = document.documentElement.getAttribute('data-theme') === 'light' ? 'light' : 'dark';

    this.elControls = null;
    this.elTimeView = null;
    this.elPsdView = null;
    this.elToolbar = null;
    this.elScopeControls = null;
    this.elYAxisControls = null;
    this.elSignalTitle = null;
    this.elSpectrumChart = null;
    this.elBandChart = null;
    this.elVarianceChart = null;
    this.elBandTitle = null;
    this.elBandMetricControls = null;
    this.elSettingsPopover = null;
    this.elLegend = null;
    this.elAnalysisModeSection = null;
    this.elVarianceYAxisDynamic = null;
    this.elVarianceYAxisRange = null;
    this.elVarianceYAxisPill = null;
    this.elTopomapSection = null;
    this.elQualitySettings = null;
    this.elQualityAutomatic = null;
    this.elQualitySummary = null;
    this.elQualityDetails = null;
    this.elQualityManage = null;
    this.elQualityError = null;

    this.spectrumChart = null;
    this.bandChart = null;
    this.varianceChart = null;
    this.topomap = null;
    this.toggleBtn = null;
    this.settingsToggleBtn = null;
    this.elMapHint = null;
    this.hintWarnTimer = null;
    this.triggerStartBtn = null;
    this.triggerStopBtn = null;
    this.bandMetricButtons = [];
    this.onModeChange = null;
    this.legendSignature = '';

    this.triggerEnabled = !!triggerEnabled;
    this.triggerActive = triggerActive === null || triggerActive === undefined ? null : !!triggerActive;
    this.triggerPending = false;
    this.electrodePositions = electrodePositions && typeof electrodePositions === 'object' ? electrodePositions : null;
    this.electrodeAliases = electrodeAliases && typeof electrodeAliases === 'object' ? electrodeAliases : null;
    try {
      const storedDynamic = localStorage.getItem(VARIANCE_YAXIS_DYNAMIC_KEY);
      const storedMax = localStorage.getItem(VARIANCE_YAXIS_FIXED_MAX_KEY);
      if (storedDynamic !== null) this.varianceYAxisDynamicEnabled = storedDynamic === '1';
      if (storedMax !== null) {
        const value = Number(storedMax);
        if (Number.isFinite(value) && value > 0) {
          this.varianceYAxisFixedMax = Math.min(
            10 ** VARIANCE_YAXIS_EXPONENT_MAX,
            Math.max(10 ** VARIANCE_YAXIS_EXPONENT_MIN, value),
          );
        }
      }
    } catch (_) {}
  }

  mount({ controlsId, timeViewId, psdViewId, chartId, bandChartId, varianceChartId, toolbarId, scopeControlsId, yAxisControlsId, onModeChange }) {
    this.disposed = false;
    this.elControls = document.getElementById(controlsId);
    this.elTimeView = document.getElementById(timeViewId);
    this.elPsdView = document.getElementById(psdViewId);
    this.elToolbar = document.getElementById(toolbarId);
    this.elScopeControls = document.getElementById(scopeControlsId);
    this.elYAxisControls = document.getElementById(yAxisControlsId);
    this.elSignalTitle = document.getElementById('eeg-signal-title');
    this.elSpectrumChart = document.getElementById(chartId);
    this.elBandChart = document.getElementById(bandChartId);
    this.elVarianceChart = document.getElementById(varianceChartId);
    this.elBandTitle = document.getElementById('band-power-title');
    this.elBandMetricControls = document.getElementById('band-metric-controls');
    this.onModeChange = typeof onModeChange === 'function' ? onModeChange : null;

    this._buildControls();
    this._buildToolbar();
    this._buildSettingsPopover();
    this._buildBandMetricControls();
    this._initTopomap();
    this._syncScopeControls();
    this._initCharts();
    this.setMode(this.mode, true);
    this._loadChannelQuality();
  }

  setTheme(theme) {
    this.theme = String(theme || 'light') === 'light' ? 'light' : 'dark';
    this._renderIfReady();
  }

  resize() {
    for (const chart of [this.spectrumChart, this.bandChart, this.varianceChart]) {
      if (!chart) continue;
      try { chart.resize(); } catch (_) {}
    }
  }

  setMode(mode, silent = false) {
    const next = mode === 'psd' ? 'psd' : 'time';
    this.mode = next;
    this._setSettingsPopoverOpen(false);
    if (this.elTimeView) this.elTimeView.classList.toggle('eeg-view--hidden', next !== 'time');
    if (this.elPsdView) this.elPsdView.classList.toggle('eeg-view--hidden', next !== 'psd');
    if (this.elSignalTitle) {
      this.elSignalTitle.textContent = next === 'psd'
        ? '脑电功率谱与频带特征（频域）'
        : '实时脑电信号（时域）';
    }
    this._syncButtons();
    if (!silent && this.onModeChange) this.onModeChange(next);
    if (next === 'psd') this.connect();
    else this.close();
    if (next === 'psd') this._renderIfReady();
    requestAnimationFrame(() => this.resize());
  }

  connect() {
    this._connectStream('psd');
    this._connectStream('variance');
  }

  _connectStream(kind) {
    if (this.disposed || this.mode !== 'psd') return;
    const socketKey = kind === 'variance' ? 'varianceWs' : 'psdWs';
    const timerKey = kind === 'variance' ? 'varianceReconnectTimer' : 'psdReconnectTimer';
    const attemptKey = kind === 'variance' ? 'varianceReconnectAttempt' : 'psdReconnectAttempt';
    if (this[socketKey]) return;
    this._clearReconnectTimer(kind);
    const proto = window.location.protocol === 'https:' ? 'wss' : 'ws';
    let socket;
    try {
      socket = new WebSocket(`${proto}://${window.location.host}/ws/${kind}`);
    } catch (_) {
      this._scheduleReconnect(kind);
      return;
    }
    this[socketKey] = socket;
    socket.onmessage = (event) => {
      if (this[socketKey] !== socket || this.disposed || this.mode !== 'psd') return;
      try {
        const msg = JSON.parse(event.data);
        if (kind === 'psd' && msg && msg.type === 'psd_data' && msg.data) {
          this[attemptKey] = 0;
          this.psdPayload = msg.data;
          this._renderIfReady();
        }
        if (kind === 'variance' && msg && msg.type === 'variance_data' && msg.data) {
          this[attemptKey] = 0;
          this.variancePayload = msg.data;
          if (msg.data.quality && typeof msg.data.quality === 'object') {
            this.channelQuality = msg.data.quality;
            this._renderChannelQuality();
          }
          this._appendVarianceHistory(msg.data);
          this._renderIfReady();
        }
      } catch (_) {}
    };
    socket.onclose = () => {
      if (this[socketKey] !== socket) return;
      this[socketKey] = null;
      if (this.mode === 'psd' && !this.disposed) this._scheduleReconnect(kind);
    };
    this[timerKey] = null;
  }

  close() {
    this._clearReconnectTimer('psd');
    this._clearReconnectTimer('variance');
    this.psdReconnectAttempt = 0;
    this.varianceReconnectAttempt = 0;
    for (const key of ['psdWs', 'varianceWs']) {
      const socket = this[key];
      this[key] = null;
      if (socket) {
        try { socket.close(); } catch (_) {}
      }
    }
    this._setSettingsPopoverOpen(false);
  }

  dispose() {
    this.disposed = true;
    this.close();
    this._clearMapHintWarn();
    for (const chart of [this.spectrumChart, this.bandChart, this.varianceChart]) {
      if (!chart) continue;
      try { chart.dispose(); } catch (_) {}
    }
    this.spectrumChart = null;
    this.bandChart = null;
    this.varianceChart = null;
    this.topomap = null;
  }

  _clearReconnectTimer(kind) {
    const timerKey = kind === 'variance' ? 'varianceReconnectTimer' : 'psdReconnectTimer';
    if (this[timerKey] === null) return;
    window.clearTimeout(this[timerKey]);
    this[timerKey] = null;
  }

  _scheduleReconnect(kind) {
    const socketKey = kind === 'variance' ? 'varianceWs' : 'psdWs';
    const timerKey = kind === 'variance' ? 'varianceReconnectTimer' : 'psdReconnectTimer';
    const attemptKey = kind === 'variance' ? 'varianceReconnectAttempt' : 'psdReconnectAttempt';
    if (this.disposed || this.mode !== 'psd' || this[socketKey] || this[timerKey] !== null) return;
    const delayMs = Math.min(5000, 750 * (2 ** Math.min(this[attemptKey], 3)));
    this[attemptKey] += 1;
    this[timerKey] = window.setTimeout(() => {
      this[timerKey] = null;
      this._connectStream(kind);
    }, delayMs);
  }

  _buildControls() {
    if (!this.elControls) return;
    this.elControls.innerHTML = '';
    const wrap = document.createElement('div');
    wrap.className = 'eeg-view-controls-row';

    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'btn btn--ghost eeg-mode-toggle-btn';
    btn.setAttribute('aria-pressed', 'false');
    btn.textContent = '时域/频域';
    btn.onclick = () => this.setMode(this.mode === 'psd' ? 'time' : 'psd');
    wrap.appendChild(btn);

    const btnStart = document.createElement('button');
    btnStart.type = 'button';
    btnStart.className = 'btn btn--ghost eeg-view-btn eeg-trigger-start-btn';
    btnStart.textContent = '开始trigger';
    btnStart.onclick = async () => this._sendTriggerStart();
    wrap.appendChild(btnStart);

    const btnStop = document.createElement('button');
    btnStop.type = 'button';
    btnStop.className = 'btn btn--ghost eeg-view-btn eeg-trigger-stop-btn';
    btnStop.textContent = '停止trigger';
    btnStop.onclick = async () => this._sendTriggerStop();
    wrap.appendChild(btnStop);

    this.elControls.appendChild(wrap);
    this.toggleBtn = btn;
    this.triggerStartBtn = btnStart;
    this.triggerStopBtn = btnStop;
    this._syncTriggerButtons();
  }

  _buildToolbar() {
    if (!this.elToolbar) return;
    this.elToolbar.innerHTML = '';
    if (this.elScopeControls) this.elScopeControls.innerHTML = '';
    const row = document.createElement('div');
    row.className = 'band-toolbar-row';

    const legend = document.createElement('div');
    legend.className = 'band-legend';
    legend.setAttribute('aria-label', '频带范围图例');

    const metaArea = document.createElement('div');
    metaArea.className = 'spectrum-toolbar-meta';
    metaArea.appendChild(legend);

    row.appendChild(metaArea);
    this.elToolbar.appendChild(row);

    this.elLegend = legend;
    this._renderBandLegend(FALLBACK_BANDS);
  }

  _buildSettingsPopover() {
    const wrap = this.elScopeControls;
    if (!wrap) return;
    wrap.innerHTML = '';

    const toggleBtn = document.createElement('button');
    toggleBtn.type = 'button';
    toggleBtn.className = 'btn btn--sm btn--icon btn--ghost eeg-settings-toggle-btn';
    toggleBtn.innerHTML = GEAR_SVG;
    toggleBtn.title = '频谱设置';
    toggleBtn.setAttribute('aria-expanded', 'false');
    wrap.appendChild(toggleBtn);

    const popover = document.createElement('aside');
    popover.className = 'eeg-settings-popover eeg-psd-settings-popover';
    popover.hidden = true;

    const head = document.createElement('div');
    head.className = 'eeg-settings-popover-head';
    const headTitle = document.createElement('div');
    headTitle.className = 'band-panel-title';
    headTitle.textContent = '频谱设置';
    head.appendChild(headTitle);
    popover.appendChild(head);

    const body = document.createElement('div');
    body.className = 'eeg-settings-popover-body';

    const analysisRow = document.createElement('div');
    analysisRow.className = 'eeg-settings-row eeg-analysis-mode-row';
    const analysisLabel = document.createElement('span');
    analysisLabel.className = 'eeg-settings-label';
    analysisLabel.textContent = '全通道分析';
    const analysisSwitch = document.createElement('label');
    analysisSwitch.className = 'ios-switch';
    analysisSwitch.title = '开启显示全通道平均结果，关闭后可选择单通道或双通道对比';
    const analysisInput = document.createElement('input');
    analysisInput.type = 'checkbox';
    analysisInput.checked = this.scopeMode === 'average';
    analysisInput.setAttribute('role', 'switch');
    analysisInput.setAttribute('aria-label', '全通道分析');
    analysisInput.onchange = () => this._setScopeMode(analysisInput.checked ? 'average' : 'channel');
    const analysisSlider = document.createElement('span');
    analysisSlider.className = 'ios-slider';
    analysisSwitch.append(analysisInput, analysisSlider);
    analysisRow.append(analysisLabel, analysisSwitch);
    body.appendChild(analysisRow);

    const varianceSection = document.createElement('div');
    varianceSection.className = 'eeg-settings-section eeg-variance-yaxis-section';
    const varianceTitle = document.createElement('div');
    varianceTitle.className = 'eeg-settings-section-title';
    varianceTitle.textContent = '方差趋势 Y 轴';
    varianceSection.appendChild(varianceTitle);

    const varianceDynamicRow = document.createElement('div');
    varianceDynamicRow.className = 'eeg-settings-row';
    const varianceDynamicLabel = document.createElement('span');
    varianceDynamicLabel.className = 'eeg-settings-label';
    varianceDynamicLabel.textContent = '自动量程';
    const varianceDynamicSwitch = document.createElement('label');
    varianceDynamicSwitch.className = 'ios-switch';
    varianceDynamicSwitch.title = '根据最近方差趋势自动调整 Y 轴上限';
    const varianceDynamicInput = document.createElement('input');
    varianceDynamicInput.type = 'checkbox';
    varianceDynamicInput.checked = this.varianceYAxisDynamicEnabled;
    varianceDynamicInput.setAttribute('role', 'switch');
    varianceDynamicInput.setAttribute('aria-label', '方差趋势自动量程');
    const varianceDynamicSlider = document.createElement('span');
    varianceDynamicSlider.className = 'ios-slider';
    varianceDynamicSwitch.append(varianceDynamicInput, varianceDynamicSlider);
    varianceDynamicRow.append(varianceDynamicLabel, varianceDynamicSwitch);
    varianceSection.appendChild(varianceDynamicRow);

    const varianceRangeRow = document.createElement('div');
    varianceRangeRow.className = 'eeg-settings-row';
    const varianceRangeLabel = document.createElement('span');
    varianceRangeLabel.className = 'eeg-settings-label';
    varianceRangeLabel.textContent = '固定上限';
    const varianceRangeInput = document.createElement('input');
    varianceRangeInput.type = 'range';
    varianceRangeInput.min = String(VARIANCE_YAXIS_EXPONENT_MIN);
    varianceRangeInput.max = String(VARIANCE_YAXIS_EXPONENT_MAX);
    varianceRangeInput.step = String(VARIANCE_YAXIS_EXPONENT_STEP);
    varianceRangeInput.value = String(Math.log10(this.varianceYAxisFixedMax));
    varianceRangeInput.disabled = this.varianceYAxisDynamicEnabled;
    varianceRangeInput.setAttribute('aria-label', '方差趋势固定 Y 轴上限');
    const variancePill = document.createElement('span');
    variancePill.className = 'eeg-settings-pill';
    variancePill.textContent = formatScientific(this.varianceYAxisFixedMax);
    if (this.varianceYAxisDynamicEnabled) variancePill.classList.add('is-dim');
    varianceRangeRow.append(varianceRangeLabel, varianceRangeInput, variancePill);
    varianceSection.appendChild(varianceRangeRow);
    body.appendChild(varianceSection);

    const applyVarianceYAxisUiState = () => {
      varianceRangeInput.disabled = this.varianceYAxisDynamicEnabled;
      variancePill.classList.toggle('is-dim', this.varianceYAxisDynamicEnabled);
      variancePill.textContent = formatScientific(this.varianceYAxisFixedMax);
    };
    varianceDynamicInput.onchange = () => {
      this.varianceYAxisDynamicEnabled = varianceDynamicInput.checked;
      try { localStorage.setItem(VARIANCE_YAXIS_DYNAMIC_KEY, this.varianceYAxisDynamicEnabled ? '1' : '0'); } catch (_) {}
      applyVarianceYAxisUiState();
      this._renderIfReady();
    };
    varianceRangeInput.oninput = () => {
      const exponent = Number(varianceRangeInput.value);
      if (!Number.isFinite(exponent)) return;
      const clampedExponent = Math.min(
        VARIANCE_YAXIS_EXPONENT_MAX,
        Math.max(VARIANCE_YAXIS_EXPONENT_MIN, exponent),
      );
      this.varianceYAxisFixedMax = 10 ** clampedExponent;
      varianceRangeInput.value = String(clampedExponent);
      try { localStorage.setItem(VARIANCE_YAXIS_FIXED_MAX_KEY, String(this.varianceYAxisFixedMax)); } catch (_) {}
      applyVarianceYAxisUiState();
      if (!this.varianceYAxisDynamicEnabled) this._renderIfReady();
    };

    const sec2 = document.createElement('div');
    sec2.className = 'eeg-settings-section eeg-settings-topomap-section';
    const sec2Title = document.createElement('div');
    sec2Title.className = 'eeg-settings-section-title';
    sec2Title.textContent = '选择通道';
    sec2.appendChild(sec2Title);

    const mapHint = document.createElement('div');
    mapHint.className = 'eeg-settings-hint';
    mapHint.textContent = MAP_HINT_DEFAULT;
    sec2.appendChild(mapHint);

    const mapWrap = document.createElement('div');
    mapWrap.className = 'band-channel-map topomap-canvas';
    const mapHost = document.createElement('div');
    mapHost.id = 'band-channel-topomap';
    mapHost.className = 'topomap-svg band-power-map';
    mapWrap.appendChild(mapHost);
    sec2.appendChild(mapWrap);

    const quality = document.createElement('div');
    quality.className = 'eeg-quality-settings';
    quality.hidden = this.scopeMode !== 'average';

    const qualityTitle = document.createElement('div');
    qualityTitle.className = 'eeg-settings-section-title eeg-quality-settings-title';
    qualityTitle.textContent = '通道质量';
    quality.appendChild(qualityTitle);

    const automaticRow = document.createElement('label');
    automaticRow.className = 'eeg-quality-toggle-row';
    const automaticLabel = document.createElement('span');
    automaticLabel.textContent = '自动排除异常通道';
    const automaticInput = document.createElement('input');
    automaticInput.type = 'checkbox';
    automaticInput.setAttribute('aria-label', '自动排除异常通道');
    automaticInput.onchange = () => this._setQualityAutomatic(automaticInput.checked);
    automaticRow.append(automaticLabel, automaticInput);
    quality.appendChild(automaticRow);

    const summary = document.createElement('div');
    summary.className = 'eeg-quality-summary-line';
    summary.textContent = '有效通道 --/--';
    quality.appendChild(summary);

    const details = document.createElement('div');
    details.className = 'eeg-quality-details';
    details.textContent = '等待质量数据';
    quality.appendChild(details);

    const manage = document.createElement('button');
    manage.type = 'button';
    manage.className = 'btn btn--ghost eeg-quality-manage-btn';
    manage.textContent = '管理手动排除通道';
    manage.setAttribute('aria-pressed', 'false');
    manage.onclick = () => this._setManualQualityMode(!this.manualQualityMode);
    quality.appendChild(manage);

    const error = document.createElement('div');
    error.className = 'eeg-quality-error';
    error.hidden = true;
    error.setAttribute('role', 'status');
    quality.appendChild(error);

    sec2.appendChild(quality);
    body.appendChild(sec2);

    popover.appendChild(body);
    wrap.appendChild(popover);

    toggleBtn.onclick = () => this._setSettingsPopoverOpen(popover.hidden);

    this.settingsToggleBtn = toggleBtn;
    this.elSettingsPopover = popover;
    this.elMapHint = mapHint;
    this.elAnalysisModeSection = analysisRow;
    this.elVarianceYAxisDynamic = varianceDynamicInput;
    this.elVarianceYAxisRange = varianceRangeInput;
    this.elVarianceYAxisPill = variancePill;
    this.elTopomapSection = sec2;
    this.scopeToggle = analysisInput;
    this.elQualitySettings = quality;
    this.elQualityAutomatic = automaticInput;
    this.elQualitySummary = summary;
    this.elQualityDetails = details;
    this.elQualityManage = manage;
    this.elQualityError = error;
  }

  _buildBandMetricControls() {
    if (!this.elBandMetricControls) return;
    this.elBandMetricControls.innerHTML = '';
    this.bandMetricButtons = [];

    const options = [
      { key: 'energy', label: '%', ariaLabel: '相对功率百分比', title: '显示各频段相对功率' },
      { key: 'de', label: 'DE', ariaLabel: '微分熵 DE', title: '显示各频段微分熵（Differential Entropy）' },
    ];
    for (const option of options) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'btn seg-btn band-metric-btn';
      button.dataset.metric = option.key;
      button.textContent = option.label;
      button.setAttribute('aria-label', option.ariaLabel);
      button.title = option.title;
      button.onclick = () => this._setBandMetricMode(option.key);
      this.elBandMetricControls.appendChild(button);
      this.bandMetricButtons.push(button);
    }
    this._syncBandMetricControls();
  }

  _setBandMetricMode(mode) {
    const next = ['energy', 'de'].includes(mode) ? mode : 'energy';
    if (this.bandMetricMode === next) return;
    this.bandMetricMode = next;
    this._syncBandMetricControls();
    this._renderIfReady();
  }

  _syncBandMetricControls() {
    const titles = {
      energy: '各频段相对功率',
      de: '各频段微分熵',
    };
    if (this.elBandTitle) {
      this.elBandTitle.textContent = titles[this.bandMetricMode] || titles.energy;
    }
    for (const button of this.bandMetricButtons) {
      const active = button.dataset.metric === this.bandMetricMode;
      button.classList.toggle('is-active', active);
      button.setAttribute('aria-pressed', active ? 'true' : 'false');
    }
  }

  _initTopomap() {
    const host = document.getElementById('band-channel-topomap');
    this.topomap = createSelectableTopomap(
      host,
      this.channelNames,
      this.electrodePositions,
      this.electrodeAliases,
      { maxCount: 2, minCount: 1, onReject: () => this._flashMapHintWarn() },
    );
    if (!this.topomap) return;
    this.topomap.setOnSelect((list) => this._setActiveChannels(list));
    this.topomap.setOnManualToggle((name) => this._toggleManualQualityChannel(name));
    this.topomap.setSelected(this.activeChannels);
    this._syncTopomapQuality();
  }

  _syncTopomapQuality() {
    if (!this.topomap) return;
    this.topomap.setQuality(this.scopeMode === 'average' ? this.channelQuality : null);
  }

  async _loadChannelQuality() {
    try {
      const [policy, snapshot] = await Promise.all([
        getSignalQuality(),
        getSignalQualitySnapshot(),
      ]);
      if (this.disposed) return;
      this.qualityPolicy = policy && typeof policy === 'object' ? policy : {};
      this.channelQuality = snapshot && typeof snapshot === 'object' ? snapshot : null;
      this.qualityError = '';
    } catch (error) {
      if (this.disposed) return;
      this.qualityError = error instanceof Error ? error.message : '通道质量加载失败';
    }
    this._renderChannelQuality();
  }

  async _setQualityAutomatic(enabled) {
    if (this.qualityBusy) return;
    this.qualityBusy = true;
    this.qualityError = '';
    this._renderChannelQuality();
    try {
      const policy = await setSignalQuality({ automatic_enabled: !!enabled });
      if (this.disposed) return;
      this.qualityPolicy = policy && typeof policy === 'object' ? policy : {};
      if (this.channelQuality) this.channelQuality.automatic_enabled = !!enabled;
    } catch (error) {
      if (this.disposed) return;
      this.qualityError = error instanceof Error ? error.message : '自动排除设置失败';
    } finally {
      this.qualityBusy = false;
      this._renderChannelQuality();
    }
  }

  _setManualQualityMode(enabled) {
    this.manualQualityMode = !!enabled;
    this.qualityError = '';
    if (this.topomap) {
      this.topomap.setInteractionMode(this.manualQualityMode ? 'manual-quality' : 'select');
    }
    this._syncScopeControls();
    this._renderChannelQuality();
  }

  async _toggleManualQualityChannel(name) {
    if (!this.manualQualityMode || this.qualityBusy) return;
    const snapshot = this.channelQuality && typeof this.channelQuality === 'object'
      ? this.channelQuality
      : {};
    const current = Array.isArray(snapshot.manual_excluded_channels)
      ? snapshot.manual_excluded_channels.map(String)
      : [];
    const next = current.includes(name)
      ? current.filter((item) => item !== name)
      : [...current, name];
    this.qualityBusy = true;
    this.qualityError = '';
    this._renderChannelQuality();
    try {
      const result = await setSignalQualityManualChannels(next);
      if (this.disposed) return;
      this.channelQuality = result && typeof result === 'object' ? result : snapshot;
    } catch (error) {
      if (this.disposed) return;
      this.qualityError = error instanceof Error ? error.message : '手动排除设置失败';
    } finally {
      this.qualityBusy = false;
      if (!this.disposed) this._renderChannelQuality();
    }
  }

  _renderChannelQuality() {
    const snapshot = this.channelQuality && typeof this.channelQuality === 'object'
      ? this.channelQuality
      : {};
    const policy = this.qualityPolicy && typeof this.qualityPolicy === 'object'
      ? this.qualityPolicy
      : {};
    const automaticEnabled = typeof snapshot.automatic_enabled === 'boolean'
      ? snapshot.automatic_enabled
      : !!policy.automatic_enabled;
    const total = Number.isFinite(Number(snapshot.total_channel_count))
      ? Number(snapshot.total_channel_count)
      : this.channelNames.length;
    const valid = Number.isFinite(Number(snapshot.valid_channel_count))
      ? Number(snapshot.valid_channel_count)
      : total;
    const automatic = Array.isArray(snapshot.automatic_bad_channels)
      ? snapshot.automatic_bad_channels.map(String)
      : [];
    const manual = Array.isArray(snapshot.manual_excluded_channels)
      ? snapshot.manual_excluded_channels.map(String)
      : [];
    if (this.elQualityAutomatic) {
      this.elQualityAutomatic.checked = automaticEnabled;
      this.elQualityAutomatic.disabled = this.qualityBusy;
    }
    if (this.elQualitySummary) {
      this.elQualitySummary.textContent = `有效通道 ${valid}/${total}`;
    }
    if (this.elQualityDetails) {
      this.elQualityDetails.textContent = snapshot.ready === false
        ? '质量检测预热中'
        : `自动排除 ${automatic.length ? automatic.join('、') : '无'} · 手动停用 ${manual.length ? manual.join('、') : '无'}`;
    }
    if (this.elQualityManage) {
      this.elQualityManage.disabled = this.qualityBusy;
      this.elQualityManage.classList.toggle('is-active', this.manualQualityMode);
      this.elQualityManage.setAttribute('aria-pressed', this.manualQualityMode ? 'true' : 'false');
      this.elQualityManage.textContent = this.manualQualityMode ? '完成手动排除' : '管理手动排除通道';
    }
    if (this.elQualityError) {
      this.elQualityError.hidden = !this.qualityError;
      this.elQualityError.textContent = this.qualityError;
    }
    if (this.elMapHint && this.manualQualityMode) {
      this.elMapHint.textContent = '点击电极切换手动启用或停用';
      this.elMapHint.classList.remove('is-warn');
    } else if (this.elMapHint && this.hintWarnTimer === null) {
      this._restoreMapHint();
    }
    this._syncTopomapQuality();
  }

  _setSettingsPopoverOpen(open) {
    const shouldOpen = !!open;
    if (this.elSettingsPopover) this.elSettingsPopover.hidden = !shouldOpen;
    if (this.settingsToggleBtn) {
      this.settingsToggleBtn.innerHTML = shouldOpen ? CLOSE_SVG : GEAR_SVG;
      this.settingsToggleBtn.setAttribute('aria-expanded', shouldOpen ? 'true' : 'false');
    }
    if (shouldOpen && this.topomap) this.topomap.setSelected(this.activeChannels);
    if (!shouldOpen) {
      this._setManualQualityMode(false);
      this._clearMapHintWarn();
    }
  }

  _setScopeMode(mode) {
    const next = mode === 'channel' ? 'channel' : 'average';
    this.scopeMode = next;
    if (!this.activeChannels.length && this.channelNames.length) this.activeChannels = [this.channelNames[0]];
    this._syncScopeControls();
    this._renderIfReady();
    requestAnimationFrame(() => this.resize());
  }

  _syncScopeControls() {
    const isFullChannel = this.scopeMode === 'average';
    if (this.scopeToggle) this.scopeToggle.checked = isFullChannel;
    if (this.elTopomapSection) {
      this.elTopomapSection.classList.toggle('is-dim', isFullChannel && !this.manualQualityMode);
    }
    if (this.elQualitySettings) this.elQualitySettings.hidden = !isFullChannel;
    if (!isFullChannel && this.manualQualityMode) {
      this.manualQualityMode = false;
      if (this.topomap) this.topomap.setInteractionMode('select');
    }
    this._syncTopomapQuality();
    if (this.elMapHint && this.hintWarnTimer === null && !this.manualQualityMode) {
      this._restoreMapHint();
    }
  }

  _initCharts() {
    if (!window.echarts || typeof window.echarts.init !== 'function') return;
    if (this.elSpectrumChart) this.spectrumChart = window.echarts.init(this.elSpectrumChart, null, { renderer: 'canvas' });
    if (this.elBandChart) this.bandChart = window.echarts.init(this.elBandChart, null, { renderer: 'canvas' });
    if (this.elVarianceChart) this.varianceChart = window.echarts.init(this.elVarianceChart, null, { renderer: 'canvas' });
    this._renderWaitingCharts('等待频谱数据');
  }

  _setActiveChannels(list) {
    const names = (Array.isArray(list) ? list : [list])
      .map((name) => String(name || ''))
      .filter((name, index, arr) => (
        name && this.channelNames.includes(name) && arr.indexOf(name) === index
      ));
    if (!names.length) return;
    this.activeChannels = names;
    if (this.topomap) this.topomap.setSelected(names);
    this._renderIfReady();
  }

  _flashMapHintWarn() {
    if (!this.elMapHint) return;
    if (this.hintWarnTimer !== null) window.clearTimeout(this.hintWarnTimer);
    this.elMapHint.textContent = MAP_HINT_WARN;
    this.elMapHint.classList.add('is-warn');
    this.hintWarnTimer = window.setTimeout(() => {
      this.hintWarnTimer = null;
      this._restoreMapHint();
    }, 1200);
  }

  _restoreMapHint() {
    if (!this.elMapHint) return;
    this.elMapHint.textContent = this.scopeMode === 'average'
      ? MAP_HINT_FULL_CHANNEL
      : MAP_HINT_DEFAULT;
    this.elMapHint.classList.remove('is-warn');
  }

  _clearMapHintWarn() {
    if (this.hintWarnTimer !== null) {
      window.clearTimeout(this.hintWarnTimer);
      this.hintWarnTimer = null;
    }
    this._restoreMapHint();
  }

  _syncButtons() {
    const isMonitor = this.mode === 'psd';
    if (this.toggleBtn) {
      this.toggleBtn.setAttribute('aria-pressed', isMonitor ? 'true' : 'false');
      this.toggleBtn.textContent = '时域/频域';
    }
    if (this.elToolbar) this.elToolbar.style.display = isMonitor ? '' : 'none';
    if (this.elScopeControls) this.elScopeControls.hidden = !isMonitor;
    if (this.elYAxisControls) this.elYAxisControls.hidden = isMonitor;
    this._syncTriggerButtons();
  }

  _syncTriggerButtons() {
    const enabled = !!this.triggerEnabled;
    const active = this.triggerActive;
    const pending = !!this.triggerPending;
    if (this.triggerStartBtn) this.triggerStartBtn.disabled = !enabled || pending || active === true;
    if (this.triggerStopBtn) this.triggerStopBtn.disabled = !enabled || pending || active === false;
  }

  async _sendTriggerStart() {
    if (!this.triggerEnabled || this.triggerPending) return;
    this.triggerPending = true;
    this._syncTriggerButtons();
    try {
      await triggerStart();
      this.triggerActive = true;
    } catch (error) {
      try { console.warn(error); } catch (_) {}
    } finally {
      this.triggerPending = false;
      this._syncTriggerButtons();
    }
  }

  async _sendTriggerStop() {
    if (!this.triggerEnabled || this.triggerPending) return;
    this.triggerPending = true;
    this._syncTriggerButtons();
    try {
      await triggerStop();
      this.triggerActive = false;
    } catch (error) {
      try { console.warn(error); } catch (_) {}
    } finally {
      this.triggerPending = false;
      this._syncTriggerButtons();
    }
  }

  _appendVarianceHistory(payload) {
    if (!payload || !payload.warmup || !payload.warmup.ready) return;
    const timestamp = Number(payload.ts);
    const tsMs = Number.isFinite(timestamp) ? timestamp * 1000 : Date.now();
    const average = Array.isArray(payload.average) ? payload.average.map(Number) : [];
    const channels = {};
    if (payload.channels && typeof payload.channels === 'object') {
      for (const [name, values] of Object.entries(payload.channels)) {
        if (Array.isArray(values)) channels[name] = values.map(Number);
      }
    }
    this.varianceHistory.push({ tsMs, average, channels });
    const cutoff = tsMs - VARIANCE_TREND_WINDOW_MS;
    this.varianceHistory = this.varianceHistory.filter((entry) => entry.tsMs >= cutoff);
  }

  _varianceSources() {
    if (this.scopeMode === 'average') return [{ name: '全通道平均', key: 'average' }];
    return this.activeChannels.map((name) => ({ name, key: name }));
  }

  _renderVarianceChart(bands) {
    if (!this.varianceChart) return;
    if (!this.varianceHistory.length) {
      this.varianceChart.setOption(this._waitingOption('等待方差趋势数据'), true, false);
      return;
    }
    const colors = this._chartTheme();
    const sources = this._varianceSources();
    const latestTs = this.varianceHistory[this.varianceHistory.length - 1].tsMs;
    const series = [];
    let observedMax = 0;
    sources.forEach((source, sourceIndex) => {
      bands.forEach((band, bandIndex) => {
        const data = this.varianceHistory.map((entry) => {
          const values = source.key === 'average' ? entry.average : entry.channels[source.key];
          const value = Array.isArray(values) ? Number(values[bandIndex]) : NaN;
          return Number.isFinite(value)
            ? [(entry.tsMs - latestTs) / 1000, value]
            : null;
        }).filter(Boolean);
        for (const point of data) {
          const value = Number(point[1]);
          if (Number.isFinite(value)) observedMax = Math.max(observedMax, value);
        }
        series.push({
          name: sources.length > 1 ? `${source.name} · ${band.name}` : band.name,
          type: 'line',
          data,
          showSymbol: false,
          smooth: 0.12,
          connectNulls: false,
          lineStyle: {
            width: sourceIndex === 0 ? 2 : 1.6,
            type: sourceIndex === 0 ? 'solid' : 'dashed',
            color: BAND_COLORS[band.key] || '#38BDF8',
          },
          itemStyle: { color: BAND_COLORS[band.key] || '#38BDF8' },
        });
      });
    });
    const dynamicMax = observedMax > 0
      ? observedMax * 1.25
      : 1;
    const yAxisMax = this.varianceYAxisDynamicEnabled
      ? dynamicMax
      : Math.max(10 ** VARIANCE_YAXIS_EXPONENT_MIN, Number(this.varianceYAxisFixedMax) || VARIANCE_YAXIS_FIXED_MAX_DEFAULT);
    this.varianceChart.setOption({
      backgroundColor: 'transparent',
      grid: { top: 18, right: 18, bottom: 42, left: 58, containLabel: false },
      legend: { show: false },
      tooltip: {
        trigger: 'axis',
        backgroundColor: colors.tooltipBg,
        borderColor: colors.tooltipBorder,
        textStyle: { color: colors.axis },
        formatter: (params) => {
          const items = Array.isArray(params) ? params : [params];
          if (!items.length) return '';
          const seconds = Number(items[0].value[0]);
          const heading = seconds >= -0.05 ? '当前' : `${Math.abs(seconds).toFixed(1)} 秒前`;
          const rows = items.map((item) => `${item.marker || ''}${escapeHtml(item.seriesName)}：<strong>${formatScientific(item.value[1])}</strong>`).join('<br>');
          return `<div class="band-tooltip-title">${heading}</div>${rows}`;
        },
      },
      xAxis: {
        type: 'value',
        min: -30,
        max: 0,
        axisLine: { lineStyle: { color: colors.split } },
        axisTick: { show: false },
        axisLabel: { color: colors.axis, formatter: (value) => (Number(value) === 0 ? '现在' : `${Math.abs(Number(value))}s`) },
        splitLine: { lineStyle: { color: colors.split, type: 'dashed' } },
      },
      yAxis: {
        type: 'value',
        min: 0,
        max: yAxisMax,
        scale: false,
        axisLine: { show: false },
        axisTick: { show: false },
        axisLabel: { color: colors.axis, formatter: (value) => formatScientific(value, 1) },
        splitLine: { lineStyle: { color: colors.split, type: 'dashed' } },
      },
      series,
      animation: false,
    }, true, false);
  }

  _bandsFromPayload() {
    const raw = this.psdPayload && this.psdPayload.band_power
      ? this.psdPayload.band_power.bands
      : (this.variancePayload ? this.variancePayload.bands : null);
    if (!Array.isArray(raw) || !raw.length) return FALLBACK_BANDS;
    return raw.map((band, index) => ({
      key: String(band && band.key ? band.key : `band-${index}`),
      name: String(band && band.name ? band.name : `Band ${index + 1}`),
      symbol: String(band && band.symbol ? band.symbol : ''),
      fmin_hz: Number(band && band.fmin_hz),
      fmax_hz: Number(band && band.fmax_hz),
    }));
  }

  _renderBandLegend(bands) {
    if (!this.elLegend) return;
    const signature = bands.map((band) => `${band.key}:${band.fmin_hz}:${band.fmax_hz}`).join('|');
    if (signature === this.legendSignature) return;
    this.legendSignature = signature;
    this.elLegend.innerHTML = '';
    for (const band of bands) {
      const item = document.createElement('span');
      item.className = 'band-legend-item';
      const dot = document.createElement('span');
      dot.className = 'band-legend-dot';
      dot.style.backgroundColor = BAND_COLORS[band.key] || '#38BDF8';
      const label = document.createElement('span');
      label.textContent = `${band.name} ${formatHz(band.fmin_hz)}–${formatHz(band.fmax_hz)} Hz`;
      item.appendChild(dot);
      item.appendChild(label);
      this.elLegend.appendChild(item);
    }
  }

  _displayRange() {
    return { fmin: PSD_DISPLAY_MIN_HZ, fmax: PSD_DISPLAY_MAX_HZ };
  }

  _normalizeBandRow(row, bandCount) {
    if (!row || typeof row !== 'object') return null;
    const absolute = Array.isArray(row.absolute)
      ? row.absolute.slice(0, bandCount).map((value) => Math.max(0, Number(value) || 0))
      : [];
    let values = Array.isArray(row.relative_pct)
      ? row.relative_pct.slice(0, bandCount).map((value) => Math.max(0, Number(value) || 0))
      : [];
    if (values.length < bandCount && absolute.length >= bandCount) {
      const sum = absolute.reduce((total, value) => total + value, 0);
      values = absolute.map((value) => (sum > 0 ? (value * 100) / sum : 0));
    }
    if (values.length < bandCount) return null;
    const sum = values.reduce((total, value) => total + value, 0);
    if (sum > 0) values = values.map((value) => (value * 100) / sum);
    const normalizeMetric = (raw) => (Array.isArray(raw)
      ? raw.slice(0, bandCount).map((value) => {
        if (value === null || value === undefined) return null;
        const number = Number(value);
        return Number.isFinite(number) ? number : null;
      })
      : []);
    const causalVariance = normalizeMetric(row.causal_variance);
    const differentialEntropy = normalizeMetric(row.differential_entropy);
    return {
      values,
      absolute: absolute.length >= bandCount ? absolute : new Array(bandCount).fill(null),
      causalVariance: causalVariance.length >= bandCount
        ? causalVariance
        : new Array(bandCount).fill(null),
      differentialEntropy: differentialEntropy.length >= bandCount
        ? differentialEntropy
        : new Array(bandCount).fill(null),
      totalPower: Number.isFinite(Number(row.total)) ? Number(row.total) : null,
    };
  }

  _averageBandData(bandCount) {
    const average = this.psdPayload && this.psdPayload.average;
    if (average && average.band_power) {
      const normalized = this._normalizeBandRow(average.band_power, bandCount);
      if (normalized) return normalized;
    }

    const channels = this.psdPayload && this.psdPayload.band_power
      ? this.psdPayload.band_power.channels
      : null;
    if (!channels || typeof channels !== 'object') return null;
    const rows = this.channelNames
      .map((name) => channels[name])
      .filter((row) => row && Array.isArray(row.absolute) && row.absolute.length >= bandCount);
    if (!rows.length) return null;
    const absolute = new Array(bandCount).fill(0);
    for (const row of rows) {
      for (let i = 0; i < bandCount; i++) absolute[i] += Math.max(0, Number(row.absolute[i]) || 0);
    }
    for (let i = 0; i < bandCount; i++) absolute[i] /= rows.length;
    const total = absolute.reduce((sum, value) => sum + value, 0);
    const averageMetric = (field) => {
      const result = new Array(bandCount).fill(null);
      for (let i = 0; i < bandCount; i++) {
        const values = rows
          .map((row) => Array.isArray(row[field]) ? row[field][i] : null)
          .filter((value) => value !== null && value !== undefined && Number.isFinite(Number(value)))
          .map(Number);
        if (values.length) result[i] = values.reduce((sum, value) => sum + value, 0) / values.length;
      }
      return result;
    };
    return this._normalizeBandRow({
      absolute,
      causal_variance: averageMetric('causal_variance'),
      differential_entropy: averageMetric('differential_entropy'),
      total,
    }, bandCount);
  }

  _sourceBandData(bandCount) {
    if (this.scopeMode === 'average') {
      const row = this._averageBandData(bandCount);
      return row ? [{ name: '全通道平均', bandPower: row }] : null;
    }
    const channels = this.psdPayload && this.psdPayload.band_power
      ? this.psdPayload.band_power.channels
      : null;
    const rows = [];
    for (const name of this.activeChannels) {
      const row = this._normalizeBandRow(channels ? channels[name] : null, bandCount);
      if (!row) return null;
      rows.push({ name, bandPower: row });
    }
    return rows.length ? rows : null;
  }

  _sourceSpectrum() {
    const payload = this.psdPayload;
    if (!payload || !Array.isArray(payload.freq_hz)) return null;
    const targetLength = payload.freq_hz.length;
    if (this.scopeMode === 'channel') {
      const sources = [];
      for (const name of this.activeChannels) {
        const values = payload.channels && payload.channels[name];
        if (!Array.isArray(values) || values.length !== targetLength) return null;
        sources.push({ name, data: values.map(Number) });
      }
      return sources.length ? sources : null;
    }
    const average = payload.average && payload.average.spectrum;
    if (Array.isArray(average) && average.length === targetLength) {
      return [{ name: '全通道平均', data: average.map(Number) }];
    }

    const rows = this.channelNames
      .map((name) => payload.channels && payload.channels[name])
      .filter((values) => Array.isArray(values) && values.length === targetLength);
    if (!rows.length) return null;
    const isDb = String(payload.unit || '').toLowerCase() === 'db';
    const result = new Array(targetLength).fill(0);
    for (let i = 0; i < targetLength; i++) {
      let sum = 0;
      for (const values of rows) {
        const value = Number(values[i]);
        sum += isDb ? 10 ** (value / 10) : value;
      }
      const mean = sum / rows.length;
      result[i] = isDb ? 10 * Math.log10(Math.max(mean, 1e-20)) : mean;
    }
    return [{ name: '全通道平均', data: result }];
  }

  _chartTheme() {
    const isLight = this.theme === 'light';
    return {
      axis: isLight ? '#273449' : 'rgba(236, 242, 255, 0.78)',
      muted: isLight ? '#64748B' : 'rgba(196, 210, 230, 0.62)',
      split: isLight ? 'rgba(39, 52, 73, 0.11)' : 'rgba(210, 226, 248, 0.09)',
      tooltipBg: isLight ? 'rgba(255, 255, 255, 0.98)' : 'rgba(8, 13, 23, 0.97)',
      tooltipBorder: isLight ? 'rgba(39, 52, 73, 0.16)' : 'rgba(148, 184, 225, 0.22)',
      line: isLight ? '#118AB2' : '#38BDF8',
    };
  }

  // 双通道对比专用：蓝/橙色盲友好组合，与 BAND_COLORS 频段色不重叠
  _channelPairColors() {
    return this.theme === 'light' ? ['#118AB2', '#F76707'] : ['#38BDF8', '#FB923C'];
  }

  _waitingOption(message) {
    const colors = this._chartTheme();
    return {
      backgroundColor: 'transparent',
      title: {
        text: message,
        left: 'center',
        top: 'middle',
        textStyle: { color: colors.muted, fontSize: 13, fontWeight: 650 },
      },
      xAxis: { show: false },
      yAxis: { show: false },
      series: [],
      animation: false,
    };
  }

  _renderWaitingCharts(message) {
    const option = this._waitingOption(message);
    if (this.spectrumChart) this.spectrumChart.setOption(option, true, false);
    if (this.bandChart) this.bandChart.setOption(option, true, false);
    if (this.varianceChart) this.varianceChart.setOption(this._waitingOption('等待方差趋势数据'), true, false);
  }

  _renderIfReady() {
    const payload = this.psdPayload;
    if (!payload || !Array.isArray(payload.freq_hz) || !payload.freq_hz.length) {
      this._renderWaitingCharts('等待频谱数据');
      return;
    }
    const bands = this._bandsFromPayload();
    const sources = this._sourceSpectrum();
    const bandRows = this._sourceBandData(bands.length);
    if (!sources || !sources.length || !bandRows || !bandRows.length) {
      this._renderWaitingCharts('当前数据范围暂无可用数据');
      return;
    }
    this._renderBandLegend(bands);
    this._renderSpectrumChart(bands, sources);
    this._renderBandChart(bands, bandRows);
    this._renderVarianceChart(bands);
  }

  _renderSpectrumChart(bands, sources) {
    if (!this.spectrumChart || !this.psdPayload) return;
    const frequencies = this.psdPayload.freq_hz;
    const range = this._displayRange();
    const unit = String(this.psdPayload.unit || '');
    const colors = this._chartTheme();
    const pair = sources.length > 1;
    const seriesColors = pair ? this._channelPairColors() : [colors.line];
    const markAreaData = bands.map((band) => ([
      {
        name: band.name,
        xAxis: Number(band.fmin_hz),
        itemStyle: { color: hexToRgba(BAND_COLORS[band.key] || '#38BDF8', this.theme === 'light' ? 0.12 : 0.09) },
      },
      { xAxis: Number(band.fmax_hz) },
    ]));

    const series = sources.map((source, seriesIndex) => ({
      name: source.name,
      type: 'line',
      data: frequencies.map((frequency, index) => [Number(frequency), Number(source.data[index])]),
      showSymbol: false,
      smooth: 0.08,
      sampling: 'lttb',
      lineStyle: { width: 2, color: seriesColors[seriesIndex] },
      itemStyle: { color: seriesColors[seriesIndex] },
      markArea: seriesIndex === 0
        ? { silent: true, label: { show: false }, data: markAreaData }
        : undefined,
    }));

    this.spectrumChart.setOption({
      backgroundColor: 'transparent',
      title: { show: false },
      grid: { top: pair ? 26 : 12, right: 18, bottom: 42, left: 58, containLabel: false },
      legend: pair ? {
        show: true,
        right: 6,
        top: 2,
        icon: 'roundRect',
        itemWidth: 12,
        itemHeight: 3,
        itemGap: 12,
        textStyle: { color: colors.axis, fontSize: 11, fontWeight: 700 },
      } : { show: false },
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'cross', label: { backgroundColor: colors.axis } },
        backgroundColor: colors.tooltipBg,
        borderColor: colors.tooltipBorder,
        textStyle: { color: colors.axis },
        formatter: (params) => {
          const items = (Array.isArray(params) ? params : [params])
            .filter((item) => item && Array.isArray(item.value));
          if (!items.length) return '';
          const freqLabel = `${Number(items[0].value[0]).toFixed(1)} Hz`;
          if (items.length === 1) {
            return `<div class="band-tooltip-title">${escapeHtml(items[0].seriesName)}</div><div>${freqLabel}</div><strong>${Number(items[0].value[1]).toFixed(2)} ${escapeHtml(unit)}</strong>`;
          }
          const rows = items.map((item) => (
            `<div>${item.marker || ''}${escapeHtml(item.seriesName)}：<strong>${Number(item.value[1]).toFixed(2)} ${escapeHtml(unit)}</strong></div>`
          )).join('');
          return `<div class="band-tooltip-title">${freqLabel}</div>${rows}`;
        },
      },
      xAxis: {
        type: 'value',
        min: range.fmin,
        max: range.fmax,
        name: '频率（Hz）',
        nameLocation: 'middle',
        nameGap: 28,
        nameTextStyle: { color: colors.axis, fontWeight: 750 },
        axisLine: { lineStyle: { color: colors.split } },
        axisTick: { show: false },
        axisLabel: { color: colors.axis },
        splitLine: { lineStyle: { color: colors.split, type: 'dashed' } },
      },
      yAxis: {
        type: unit.toLowerCase() === 'db' ? 'value' : 'log',
        scale: true,
        name: unit ? `PSD (${unit})` : 'PSD',
        nameLocation: 'middle',
        nameGap: 42,
        nameTextStyle: { color: colors.axis, fontWeight: 750 },
        axisLine: { show: false },
        axisTick: { show: false },
        axisLabel: { color: colors.axis },
        splitLine: { lineStyle: { color: colors.split, type: 'dashed' } },
      },
      series,
      animation: false,
    }, true, false);
  }

  _renderBandChart(bands, rows) {
    if (!this.bandChart) return;
    const colors = this._chartTheme();
    const isDe = this.bandMetricMode === 'de';
    const isVariance = this.bandMetricMode === 'variance';
    const metricUnit = isDe ? 'nat' : '%';
    const metricOf = (entry) => {
      if (isDe) return entry.bandPower.differentialEntropy;
      if (isVariance) return entry.bandPower.causalVariance;
      return entry.bandPower.values;
    };
    const usable = rows.every((entry) => {
      const metricValues = metricOf(entry);
      return Array.isArray(metricValues)
        && metricValues.length >= bands.length
        && !metricValues.some((value) => (
          value === null || value === undefined || !Number.isFinite(Number(value))
        ));
    });
    if (!usable) {
      const message = isDe
        ? '当前数据无法计算微分熵'
        : (isVariance ? '当前数据无法计算因果方差' : '当前数据范围暂无可用数据');
      this.bandChart.setOption(this._waitingOption(message), true, false);
      return;
    }

    // 合并所有数据源（单通道或双通道）的极值计算 y 轴范围
    const allValues = rows.flatMap((entry) => metricOf(entry).map(Number));
    let yMin = 0;
    let yMax = 100;
    if (isDe) {
      const minValue = Math.min(...allValues);
      const maxValue = Math.max(...allValues);
      const padding = Math.max(0.5, (maxValue - minValue) * 0.18);
      yMin = minValue < 0 ? Math.floor((minValue - padding) * 2) / 2 : 0;
      yMax = maxValue > 0 ? Math.ceil((maxValue + padding) * 2) / 2 : 0;
      if (yMax <= yMin) yMax = yMin + 1;
    } else if (isVariance) {
      const maxValue = Math.max(0, ...allValues);
      yMax = maxValue > 0 ? maxValue * 1.25 : 1;
    } else {
      const maxValue = Math.max(0, ...allValues);
      yMax = Math.min(100, Math.max(50, Math.ceil((maxValue * 1.25) / 10) * 10));
    }

    const pair = rows.length > 1;
    const pairColors = pair ? this._channelPairColors() : [];
    const categories = bands.map((band) => `${band.name}`);
    const labelFormatter = (item) => {
      if (isDe) return Number(item.value).toFixed(2);
      if (isVariance) return formatScientific(item.value);
      return `${Number(item.value).toFixed(1)}%`;
    };

    const buildBarData = (entry, seriesColor) => bands.map((band, index) => {
      const rawValue = Number(metricOf(entry)[index]);
      const value = isVariance ? rawValue : Number(rawValue.toFixed(3));
      return {
        value,
        itemStyle: {
          color: seriesColor || BAND_COLORS[band.key] || '#38BDF8',
          borderRadius: value >= 0 ? [7, 7, 2, 2] : [2, 2, 7, 7],
        },
        label: isDe ? { position: value >= 0 ? 'top' : 'bottom' } : undefined,
      };
    });

    const series = pair
      ? rows.map((entry, seriesIndex) => ({
        name: entry.name,
        type: 'bar',
        data: buildBarData(entry, pairColors[seriesIndex]),
        // series 级颜色：供图例与 tooltip marker 使用（柱体本身仍由 data item 级 itemStyle 设色）
        itemStyle: { color: pairColors[seriesIndex] },
        barGap: '35%',
        barMaxWidth: 44,
        label: {
          show: true,
          position: 'top',
          fontSize: 10,
          color: pairColors[seriesIndex],
          fontWeight: 800,
          formatter: labelFormatter,
        },
      }))
      : [{
        name: rows[0].name,
        type: 'bar',
        data: buildBarData(rows[0], null),
        barMaxWidth: 68,
        label: {
          show: true,
          position: 'top',
          color: colors.axis,
          fontWeight: 800,
          formatter: labelFormatter,
        },
      }];

    const tooltip = pair
      ? {
        trigger: 'axis',
        axisPointer: { type: 'shadow', shadowStyle: { color: 'rgba(148, 184, 225, 0.08)' } },
        backgroundColor: colors.tooltipBg,
        borderColor: colors.tooltipBorder,
        textStyle: { color: colors.axis },
        formatter: (params) => {
          const items = Array.isArray(params) ? params : [params];
          if (!items.length) return '';
          const band = bands[items[0].dataIndex];
          if (!band) return '';
          const detail = items.map((item) => {
            const entry = rows.find((row) => row.name === item.seriesName) || rows[item.seriesIndex];
            const absolute = entry ? entry.bandPower.absolute[item.dataIndex] : null;
            const power = (absolute === null || absolute === undefined)
              ? ''
              : ` · 功率 ${formatPower(absolute)} μV²`;
            const metric = isDe
              ? `${Number(item.value).toFixed(3)} ${metricUnit}`
              : (isVariance
                ? formatScientific(item.value)
                : `${Number(item.value).toFixed(1)}${metricUnit}`);
            return `<div>${item.marker || ''}${escapeHtml(item.seriesName)}：<strong>${metric}</strong>${power}</div>`;
          }).join('');
          return `<div class="band-tooltip-title">${escapeHtml(band.name)}</div><div>${formatHz(band.fmin_hz)}–${formatHz(band.fmax_hz)} Hz</div>${detail}`;
        },
      }
      : {
        trigger: 'item',
        backgroundColor: colors.tooltipBg,
        borderColor: colors.tooltipBorder,
        textStyle: { color: colors.axis },
        formatter: (item) => {
          const band = bands[item.dataIndex];
          const absolute = rows[0].bandPower.absolute[item.dataIndex];
          const power = absolute === null ? '' : `<div>功率 ${formatPower(absolute)} μV²</div>`;
          const metric = isDe
            ? `<strong>DE ${Number(item.value).toFixed(3)} ${metricUnit}</strong>`
            : (isVariance
              ? `<strong>Var ${formatScientific(item.value)}</strong>`
              : `<strong>${Number(item.value).toFixed(1)}${metricUnit}</strong>`);
          return `<div class="band-tooltip-title">${escapeHtml(rows[0].name)} · ${escapeHtml(band.name)}</div><div>${formatHz(band.fmin_hz)}–${formatHz(band.fmax_hz)} Hz</div>${power}${metric}`;
        },
      };

    this.bandChart.setOption({
      backgroundColor: 'transparent',
      title: { show: false },
      grid: { top: pair ? 30 : 28, right: 18, bottom: 42, left: 54, containLabel: false },
      legend: pair ? {
        show: true,
        right: 6,
        top: 0,
        icon: 'roundRect',
        itemWidth: 12,
        itemHeight: 8,
        itemGap: 12,
        textStyle: { color: colors.axis, fontSize: 11, fontWeight: 700 },
      } : { show: false },
      tooltip,
      xAxis: {
        type: 'category',
        data: categories,
        axisLine: { lineStyle: { color: colors.split } },
        axisTick: { show: false },
        // 对比模式：x 轴刻度文字按频段着色（ECharts 5 支持 axisLabel.color 回调），保留频段语义
        axisLabel: pair
          ? {
            color: (value, index) => {
              const band = bands[index];
              return (band && BAND_COLORS[band.key]) || colors.axis;
            },
            fontWeight: 750,
            interval: 0,
            margin: 13,
          }
          : { color: colors.axis, fontWeight: 750, interval: 0, margin: 13 },
      },
      yAxis: {
        type: 'value',
        min: yMin,
        max: yMax,
        name: isDe
          ? '微分熵（nat）'
          : (isVariance ? '' : '相对功率（%）'),
        nameLocation: 'middle',
        nameGap: 38,
        nameTextStyle: { color: colors.axis, fontWeight: 750 },
        axisLine: { show: false },
        axisTick: { show: false },
        axisLabel: {
          color: colors.axis,
          formatter: isVariance ? (value) => formatScientific(value, 1) : (isDe ? '{value}' : '{value}%'),
        },
        splitLine: { lineStyle: { color: colors.split, type: 'dashed' } },
      },
      series,
      animation: true,
      animationDuration: 220,
      animationDurationUpdate: 320,
      animationEasingUpdate: 'cubicOut',
    }, true, false);
  }
}
