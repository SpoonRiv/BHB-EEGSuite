/* PPG optical waveforms, separate from EEG processing.
   绘制前减去当前显示窗口内的逐通道均值（动态计算），突出交流成分。 */

const CHANNELS = [
  { key: 'green', label: '绿光', light: '#087f67', dark: '#6de4b2' },
  { key: 'red', label: '红光', light: '#c43b60', dark: '#ff9eb6' },
  { key: 'infrared', label: '近红外', light: '#405dc4', dark: '#afbeff' },
];

const PPG_ROW_HEIGHT = 170;
const PPG_PLOT_HEIGHT = 150;

export class PpgView {
  constructor({ windowSec = 2, enabled = false } = {}) {
    this.windowSec = Math.max(0.2, Number(windowSec) || 2);
    this.enabled = enabled;
    this.samples = [];
    this.session = null;
    this.lastId = 0;
    this.lastDataAt = 0;
    this.active = false;
    this.dirty = true;
    this.disposed = false;
    this.ws = null;
    this.reconnectTimer = null;
    // Y 轴量程模式：动态（跟随数据自适应）或固定（±fixedMax）。
    this.yAxisDynamic = true;
    this.yAxisFixedMax = 50;
    // 异常值过滤阈值（去均值后的交流计数绝对值上限，超过则丢弃该点；<=0 关闭过滤）。
    this.outlierThreshold = 2000;
  }

  /**
   * 设置异常值过滤阈值。
   * @param {number} next 阈值（<=0 或非法值表示关闭过滤）
   */
  setOutlierThreshold(next) {
    const t = Number(next);
    this.outlierThreshold = Number.isFinite(t) && t > 0 ? t : 0;
    this.dirty = true;
  }

  /**
   * 设置 Y 轴量程模式与固定量程值。
   * @param {{ dynamic: boolean, fixedMax: number }} next 目标状态
   */
  setYAxisMode({ dynamic, fixedMax } = {}) {
    if (typeof dynamic === 'boolean') this.yAxisDynamic = dynamic;
    const m = Number(fixedMax);
    if (Number.isFinite(m) && m > 0) this.yAxisFixedMax = m;
    this._applyYAxisMode();
  }

  /**
   * 更新显示窗口时长（秒），同步裁剪已有样本并更新 X 轴范围。
   * @param {number} nextSec 目标窗口时长
   */
  setWindowSec(nextSec) {
    const v = Math.max(0.2, Number(nextSec) || this.windowSec);
    if (v === this.windowSec) return;
    this.windowSec = v;
    if (this.samples.length) {
      const cutoff = this.samples[this.samples.length - 1].ts - this.windowSec;
      this.samples = this.samples.filter(sample => sample.ts >= cutoff).slice(-4096);
    }
    if (this.chart) this.chart.setOption({ xAxis: CHANNELS.map(() => ({ min: -this.windowSec, max: 0 })) });
    this.dirty = true;
  }

  setFilterEnabled(enabled) {
    if (this.filterStateEl) this.filterStateEl.textContent = enabled ? '带通滤波' : '未带通';
  }

  /** 将当前量程模式写入图表（动态模式下恢复自适应）。 */
  _applyYAxisMode() {
    if (!this.chart) return;
    const patch = this.yAxisDynamic
      ? { min: null, max: null, scale: true }
      : { min: -this.yAxisFixedMax, max: this.yAxisFixedMax, scale: false };
    this.chart.setOption({ yAxis: CHANNELS.map(() => patch) });
    this.dirty = true;
  }

  mount(grid) {
    if (!grid || !this.enabled) return;
    this.panel = document.createElement('section');
    this.panel.className = 'chart-container ppg-panel';
    this.panel.setAttribute('aria-label', 'PPG 三路光学波形');
    this.panel.innerHTML = `
      <div class="ppg-heading">
        <div class="ppg-heading-title">PPG 波形 <span class="ppg-filter-state">带通滤波</span></div>
        <span class="ppg-status" role="status">等待采集</span>
      </div>
      <div class="ppg-chart" id="chart-ppg"></div>`;
    // Keep PPG above the eight EEG channels while preserving the existing
    // EEG waveform cards below it.
    grid.prepend(this.panel);
    this.statusEl = this.panel.querySelector('.ppg-status');
    this.filterStateEl = this.panel.querySelector('.ppg-filter-state');
    this.chartEl = this.panel.querySelector('.ppg-chart');
    if (!window.echarts) {
      this.setStatus('波形组件未加载');
      return;
    }
    this.chart = window.echarts.init(this.chartEl, null, { renderer: 'canvas' });
    this.chart.setOption({
      animation: false,
      backgroundColor: 'transparent',
      grid: CHANNELS.map((_, i) => ({
        top: 8 + i * PPG_ROW_HEIGHT,
        height: PPG_PLOT_HEIGHT,
        left: 120,
        right: 10,
      })),
      title: CHANNELS.map((channel, i) => ({
        text: channel.label, left: 12, top: 24 + i * PPG_ROW_HEIGHT,
        textStyle: { fontSize: 12, fontWeight: 700 },
      })),
      xAxis: CHANNELS.map((_, i) => ({
        type: 'value', gridIndex: i, show: false, min: -this.windowSec, max: 0,
      })),
      yAxis: CHANNELS.map((_, i) => ({
        type: 'value', gridIndex: i, splitNumber: 1,
        ...(this.yAxisDynamic
          ? { min: null, max: null, scale: true }
          : { min: -this.yAxisFixedMax, max: this.yAxisFixedMax, scale: false }),
        axisLine: { show: false }, axisTick: { show: false },
        axisLabel: {
          fontSize: 10, margin: 10,
          // 直接显示原始计数数字，不使用科学计数法。
          formatter: value => {
            const v = Number(value);
            if (!Number.isFinite(v)) return '';
            if (Math.abs(v - Math.round(v)) < 1e-6) return String(Math.round(v));
            return String(Math.round(v * 100) / 100);
          },
        },
        splitLine: { lineStyle: { type: 'dashed' } },
      })),
      series: CHANNELS.map((channel, i) => ({
        name: channel.label, type: 'line', xAxisIndex: i, yAxisIndex: i,
        showSymbol: false, symbolSize: 4, connectNulls: false,
        lineStyle: { width: 1.6 }, data: [],
      })),
    });
    this.setTheme(document.documentElement.getAttribute('data-theme') || 'light');
    this.observer = new ResizeObserver(() => this.resize());
    this.observer.observe(this.chartEl);
    this.connect();
  }

  setStatus(text) {
    if (this.statusEl && this.statusEl.textContent !== text) this.statusEl.textContent = text;
  }

  clear() {
    this.samples = [];
    this.lastDataAt = 0;
    this.dirty = true;
    if (this.chart) this.chart.setOption({ series: CHANNELS.map(() => ({ data: [] })) });
  }

  receive(payload) {
    if (!payload || payload.type !== 'ppg_data') return;
    if (payload.session !== this.session) {
      this.clear();
      this.session = payload.session;
      this.lastId = 0;
    }
    this.active = payload.active === true;
    if (!this.active) {
      if (this.samples.length) this.clear();
      this.setStatus('等待采集');
      return;
    }
    let received = false;
    for (const sample of Array.isArray(payload.data) ? payload.data : []) {
      if (!sample || !Number.isFinite(sample.ts) || !Number.isInteger(sample.id) || sample.id <= this.lastId) continue;
      if (!CHANNELS.every(({ key }) => Number.isFinite(Number(sample[key])))) continue;
      const previous = this.samples[this.samples.length - 1];
      if (previous && sample.ts <= previous.ts) continue;
      // A pause in sensor updates should not be drawn as a continuous signal.
      if (previous && sample.ts - previous.ts > 0.5) this.samples.push({ ts: sample.ts - 0.001 });
      this.samples.push(sample);
      this.lastId = sample.id;
      received = true;
    }
    if (received) {
      const cutoff = this.samples[this.samples.length - 1].ts - this.windowSec;
      this.samples = this.samples.filter(sample => sample.ts >= cutoff).slice(-4096);
      this.lastDataAt = performance.now();
      this.dirty = true;
      this.setStatus('实时更新');
    } else if (!this.samples.length) {
      this.setStatus('等待有效 PPG 数据');
    }
  }

  render(now) {
    if (!this.chart || this.disposed) return;
    if (this.lastDataAt && now - this.lastDataAt > 1500) {
      this.clear();
      this.setStatus(this.active ? '等待有效 PPG 数据' : '等待采集');
    }
    if (!this.dirty || !this.chartEl.getBoundingClientRect().height) return;
    this.dirty = false;
    const latest = this.samples.length ? this.samples[this.samples.length - 1].ts : 0;
    // 逐通道计算当前窗口内样本的均值（动态），绘制时减去，使波形围绕 0 波动。
    const sums = { green: 0, red: 0, infrared: 0 };
    const counts = { green: 0, red: 0, infrared: 0 };
    for (const sample of this.samples) {
      for (const { key } of CHANNELS) {
        const v = Number(sample[key]);
        if (Number.isFinite(v)) { sums[key] += v; counts[key] += 1; }
      }
    }
    const means = {};
    for (const { key } of CHANNELS) means[key] = counts[key] ? sums[key] / counts[key] : 0;
    const threshold = this.outlierThreshold;
    this.chart.setOption({
      series: CHANNELS.map(({ key }) => ({
        showSymbol: this.samples.length === 1,
        // 超阈值异常点直接从序列中剔除（前后点自动相连，波形保持连续）；
        // 无数据的断点占位仍保留 null，避免把采集暂停绘成连续信号。
        data: (() => {
          const out = [];
          for (const sample of this.samples) {
            const v = Number(sample[key]);
            if (!Number.isFinite(v)) { out.push([sample.ts - latest, null]); continue; }
            const dv = v - means[key];
            if (threshold > 0 && Math.abs(dv) > threshold) continue;
            out.push([sample.ts - latest, dv]);
          }
          return out;
        })(),
      })),
    });
  }

  setTheme(theme) {
    if (!this.chart) return;
    const light = theme === 'light';
    this.chart.setOption({
      title: CHANNELS.map(channel => ({ textStyle: { color: light ? channel.light : channel.dark } })),
      yAxis: CHANNELS.map(() => ({
        axisLabel: { color: light ? '#365473' : '#b4cfe9' },
        splitLine: { lineStyle: { color: light ? 'rgba(48, 99, 156, 0.15)' : 'rgba(145, 191, 235, 0.16)' } },
      })),
      series: CHANNELS.map(channel => ({
        lineStyle: { color: light ? channel.light : channel.dark },
        itemStyle: { color: light ? channel.light : channel.dark },
      })),
    });
  }

  resize() {
    if (!this.chart || !this.chartEl.getBoundingClientRect().height) return;
    this.chart.resize();
    this.dirty = true;
  }

  connect() {
    if (this.disposed || !this.enabled || this.ws) return;
    const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
    const ws = new WebSocket(`${scheme}://${location.host}/ws/ppg`);
    this.ws = ws;
    ws.onopen = () => {
      if (this.ws !== ws) return;
      this.session = null;
      this.lastId = 0;
      this.clear();
      this.setStatus('等待有效 PPG 数据');
    };
    ws.onmessage = event => {
      if (this.ws !== ws) return;
      try { this.receive(JSON.parse(event.data)); } catch (_) {}
    };
    ws.onerror = () => ws.close();
    ws.onclose = () => {
      if (this.ws !== ws) return;
      this.ws = null;
      this.clear();
      this.setStatus('PPG 连接中…');
      if (!this.disposed) this.reconnectTimer = window.setTimeout(() => this.connect(), 1000);
    };
  }

  close() {
    window.clearTimeout(this.reconnectTimer);
    this.reconnectTimer = null;
    const ws = this.ws;
    this.ws = null;
    if (ws) ws.close();
    this.active = false;
    this.clear();
    this.setStatus('等待采集');
  }

  dispose() {
    this.disposed = true;
    this.close();
    if (this.observer) this.observer.disconnect();
    if (this.chart) this.chart.dispose();
    if (this.panel) this.panel.remove();
    this.chart = null;
  }
}
