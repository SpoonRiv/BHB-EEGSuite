/*
Copyright (c) 2026 BUAA BHB. All rights reserved.

文件功能: 前端入口（初始化路由、页面模块、状态轮询与顶部导航）
作者: Spoon , Fengye
*/

import { appMinimize, appShutdown, getConfig, getStatus } from './api.js';
import { initDevicePage } from './device.js';
import { initModePage } from './mode.js';
import { enterEegPage, leaveEegPage } from './eeg.js';
import { enterImpedancePage, leaveImpedancePage } from './impedance.js';
import { enterOfflinePage, leaveOfflinePage } from './offline.js';
import { enterTdcsPage, leaveTdcsPage } from './tdcs.js';
import { initOptionalModules } from './modules.js';
import { registerRoute, startRouter, navigate } from './router.js';
import { setConnBadge } from './ui.js';

let statusTimer = null;
let navLocked = false;
let musicLocked = false;
let serverMusicLocked = false;
const btnShineTimers = new WeakMap();
let btnShineTimeoutMs = 1100;
let lastStatusSnapshot = null;
let lastTdcsCapabilityKey = null;
let configRefreshInFlight = false;

const THEME_ICON_MOON = `
  <svg class="theme-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
    <path d="M21 14.5a8.5 8.5 0 0 1-11.5-11A9 9 0 1 0 21 14.5z" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/>
  </svg>
`;

const THEME_ICON_SUN = `
  <svg class="theme-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
    <circle cx="12" cy="12" r="4" fill="none" stroke="currentColor" stroke-width="2"/>
    <path d="M12 2v3M12 19v3M2 12h3M19 12h3M4.2 4.2l2.1 2.1M17.7 17.7l2.1 2.1M19.8 4.2l-2.1 2.1M6.3 17.7l-2.1 2.1" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
  </svg>
`;

function stripTrailingParenSuffix(text) {
  let s = String(text || '').trim();
  for (let i = 0; i < 3; i++) {
    const next = s.replace(/\s*[\(\（][^()\（\）]{0,64}[\)\）]\s*$/g, '').trim();
    if (next === s) break;
    s = next;
  }
  return s;
}

function normalizeDeviceName(name) {
  const raw = String(name || '').trim();
  const cleaned = stripTrailingParenSuffix(raw);
  return cleaned || raw || '未知设备';
}

function normalizeDeviceMessage(msg) {
  const raw = stripTrailingParenSuffix(String(msg || '').trim());
  if (!raw) return '';
  const lower = raw.toLowerCase();
  if (lower === 'not connected' || lower === 'not_connected') return '未连接';
  const replaced = raw
    .replace(/not connected/ig, '未连接')
    .replace(/timeout/ig, '超时')
    .replace(/disconnected/ig, '已断开');
  if (/[a-zA-Z]/.test(replaced)) return '';
  return replaced;
}

function setHeaderStatusText(text) {
  const el = document.getElementById('header-status-text');
  const value = String(text || '等待连接');
  if (el) el.textContent = value;
  const context = el ? el.closest('.header-context') : null;
  if (!context) return;
  const isReady = /已连接|运行中|就绪/.test(value);
  const isAlert = /失败|未响应|断开|错误/.test(value);
  context.classList.toggle('is-ready', isReady);
  context.classList.toggle('is-alert', isAlert);
}

function updateHeaderPageMeta() {
  const titleEl = document.getElementById('header-page-title');
  const descEl = document.getElementById('header-page-desc');
  if (!titleEl || !descEl) return;

  const raw = String(window.location.hash || '').trim();
  const hash = raw ? (raw.startsWith('#') ? raw : `#${raw}`) : '#device';
  const pageId = `page-${hash.replace(/^#/, '')}`;
  const pageEl = document.getElementById(pageId);

  const title = pageEl && pageEl.dataset && pageEl.dataset.pageTitle
    ? String(pageEl.dataset.pageTitle)
    : '设备准备';
  const desc = pageEl && pageEl.dataset && pageEl.dataset.pageDesc
    ? String(pageEl.dataset.pageDesc)
    : '完成设备扫描、通道配置与参考电极设置';

  titleEl.textContent = title;
  descEl.textContent = desc;
}

function applyConnBadge(deviceStatus) {
  const last = deviceStatus && deviceStatus.last ? deviceStatus.last : null;
  const configured = deviceStatus && deviceStatus.configured_name ? String(deviceStatus.configured_name) : '';
  const name = normalizeDeviceName(last && last.name ? String(last.name) : (configured || '未知设备'));
  const t = last && last.type ? String(last.type) : 'idle';
  const msg = normalizeDeviceMessage(last && last.message ? String(last.message) : '');

  if (t === 'connected' || t === 'ready') {
    setConnBadge('active', `已连接：${name}`);
    setHeaderStatusText(`已连接 ${name}`);
    return;
  }
  if (t === 'connecting') {
    setConnBadge('error', `连接中：${name}`);
    setHeaderStatusText(`连接中 ${name}`);
    return;
  }
  if (t === 'error') {
    void msg;
    setConnBadge('error', `连接失败：${name}`);
    setHeaderStatusText(`连接失败 ${name}`);
    return;
  }
  if (t === 'disconnected' || t === 'stopped') {
    setConnBadge('error', `连接已断开：${name}`);
    setHeaderStatusText(`连接已断开 ${name}`);
    return;
  }
  void configured;
  setConnBadge('error', '未连接');
  setHeaderStatusText('等待连接');
}

function applyNavLock(deviceStatus) {
  const running = Boolean(deviceStatus && deviceStatus.task_running) || musicLocked || serverMusicLocked;
  navLocked = running;
  const navDevice = document.getElementById('nav-device');
  const navMode = document.getElementById('nav-mode');
  if (navDevice) navDevice.disabled = running;
  if (navMode) navMode.disabled = running;
}

function getTdcsCapabilityKey(status) {
  const device = status && status.device ? status.device : null;
  const last = device && device.last ? device.last : null;
  const moduleInfo = device && device.module && typeof device.module === 'object' ? device.module : null;
  const cap = device && device.capabilities ? device.capabilities.tdcs : undefined;
  const t = last && last.type ? String(last.type) : '';
  const name = last && last.name ? String(last.name) : '';
  const eeg = moduleInfo && Number.isFinite(Number(moduleInfo.eeg_channels)) ? Number(moduleInfo.eeg_channels) : null;
  const stim = moduleInfo && Number.isFinite(Number(moduleInfo.stim_channels)) ? Number(moduleInfo.stim_channels) : null;
  const capStr = cap === undefined ? 'u' : String(cap);
  const eegStr = eeg === null ? 'n' : String(eeg);
  const stimStr = stim === null ? 'n' : String(stim);
  return `${t}|${name}|${capStr}|${eegStr}|${stimStr}`;
}

async function refreshConfigAndBroadcast() {
  if (configRefreshInFlight) return;
  configRefreshInFlight = true;
  try {
    const cfg = await getConfig();
    try {
      window.dispatchEvent(new CustomEvent('app:config', { detail: cfg }));
    } catch (_) {}
  } catch (_) {
  } finally {
    configRefreshInFlight = false;
  }
}

async function refreshStatusOnce() {
  try {
    const data = await getStatus();
    lastStatusSnapshot = data;
    serverMusicLocked = !!data?.music?.active;
    if (data && data.device) {
      applyConnBadge(data.device);
      applyNavLock(data.device);
    }
    try {
      window.dispatchEvent(new CustomEvent('app:status', { detail: data }));
    } catch (_) {}
    const nextKey = getTdcsCapabilityKey(data);
    if (nextKey !== lastTdcsCapabilityKey) {
      lastTdcsCapabilityKey = nextKey;
      await refreshConfigAndBroadcast();
    }
  } catch (_) {
    setConnBadge('error', '后端未响应');
    setHeaderStatusText('后端未响应');
  }
}

function startStatusPolling() {
  if (statusTimer) clearInterval(statusTimer);
  refreshStatusOnce();
  statusTimer = setInterval(refreshStatusOnce, 1000);
}

function bindHeaderNav() {
  const navDevice = document.getElementById('nav-device');
  const navMode = document.getElementById('nav-mode');
  if (navDevice) {
    navDevice.onclick = () => {
      if (navLocked) return;
      navigate('#device');
    };
  }
  if (navMode) {
    navMode.onclick = () => {
      if (navLocked) return;
      navigate('#mode');
    };
  }
}

function attemptCloseCurrentWindow() {
  // 浏览器仅允许脚本关闭部分由脚本拉起的窗口，这里做兼容性兜底尝试。
  window.requestAnimationFrame(() => {
    window.close();
    window.open('', '_self');
    window.close();
  });
}

function bindMinimizeButton() {
  const minimizeBtn = document.getElementById('app-minimize');
  if (!minimizeBtn) return;

  minimizeBtn.addEventListener('click', async () => {
    if (minimizeBtn.disabled) return;
    minimizeBtn.disabled = true;
    try {
      await appMinimize();
    } catch (error) {
      const message = error && error.message ? String(error.message) : '窗口最小化失败';
      minimizeBtn.title = message;
      minimizeBtn.setAttribute('aria-label', message);
      window.setTimeout(() => {
        minimizeBtn.title = '最小化窗口';
        minimizeBtn.setAttribute('aria-label', '最小化窗口');
      }, 2400);
    } finally {
      minimizeBtn.disabled = false;
    }
  });
}

function bindPowerButton() {
  const powerBtn = document.getElementById('app-power');
  const modal = document.getElementById('power-modal');
  const confirmBtn = document.getElementById('power-modal-confirm');
  const cancelBtn = document.getElementById('power-modal-cancel');
  if (!powerBtn || !modal || !confirmBtn || !cancelBtn) return;

  const closeModal = () => {
    modal.hidden = true;
  };

  const openModal = () => {
    modal.hidden = false;
    window.requestAnimationFrame(() => {
      confirmBtn.focus();
    });
  };

  powerBtn.addEventListener('click', () => {
    if (powerBtn.disabled) return;
    openModal();
  });

  cancelBtn.addEventListener('click', () => {
    closeModal();
  });

  modal.addEventListener('click', (event) => {
    if (event.target === modal) closeModal();
  });

  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && !modal.hidden) {
      closeModal();
    }
  });

  confirmBtn.addEventListener('click', async () => {
    closeModal();
    powerBtn.disabled = true;
    setConnBadge('error', '系统关闭中…');
    setHeaderStatusText('系统关闭中');
    try {
      await appShutdown();
    } catch (_) {
      setConnBadge('error', '关机失败');
      setHeaderStatusText('关机失败');
      powerBtn.disabled = false;
      return;
    }
    window.clearInterval(statusTimer);
    statusTimer = null;
    setConnBadge('error', '系统已关闭');
    setHeaderStatusText('系统已关闭');
    powerBtn.setAttribute('aria-label', '系统关闭中');
    attemptCloseCurrentWindow();
  });
}

function updateHeaderNavActive() {
  const navDevice = document.getElementById('nav-device');
  const navMode = document.getElementById('nav-mode');
  const raw = String(window.location.hash || '').trim();
  const hash = raw ? (raw.startsWith('#') ? raw : `#${raw}`) : '#device';
  const deviceActive = hash === '#device';
  const modeActive = ['#mode', '#eeg', '#offline', '#impedance', '#tdcs', '#music', '#music-export'].includes(hash);

  const applyBtnShine = (btn, type) => {
    if (!btn) return;
    const last = btnShineTimers.get(btn);
    if (last) clearTimeout(last);
    btn.classList.remove('btn-shine-in', 'btn-shine-out');
    void btn.offsetWidth;
    btn.classList.add(type);
    const timer = setTimeout(() => {
      btn.classList.remove(type);
      btnShineTimers.delete(btn);
    }, btnShineTimeoutMs);
    btnShineTimers.set(btn, timer);
  };

  if (navDevice) {
    const wasActive = navDevice.classList.contains('is-active');
    navDevice.classList.toggle('is-active', deviceActive);
    navDevice.setAttribute('aria-pressed', deviceActive ? 'true' : 'false');
    if (deviceActive) navDevice.setAttribute('aria-current', 'page');
    else navDevice.removeAttribute('aria-current');
    if (wasActive && !deviceActive) applyBtnShine(navDevice, 'btn-shine-out');
    if (!wasActive && deviceActive) applyBtnShine(navDevice, 'btn-shine-in');
  }
  if (navMode) {
    const wasActive = navMode.classList.contains('is-active');
    navMode.classList.toggle('is-active', modeActive);
    navMode.setAttribute('aria-pressed', modeActive ? 'true' : 'false');
    if (modeActive) navMode.setAttribute('aria-current', 'page');
    else navMode.removeAttribute('aria-current');
    if (wasActive && !modeActive) applyBtnShine(navMode, 'btn-shine-out');
    if (!wasActive && modeActive) applyBtnShine(navMode, 'btn-shine-in');
  }

  updateHeaderPageMeta();
}

function parseDurationMs(value, fallbackMs) {
  const raw = String(value || '').trim();
  if (!raw) return fallbackMs;
  if (raw.endsWith('ms')) {
    const n = Number(raw.slice(0, -2).trim());
    return Number.isFinite(n) ? n : fallbackMs;
  }
  if (raw.endsWith('s')) {
    const n = Number(raw.slice(0, -1).trim());
    return Number.isFinite(n) ? n * 1000 : fallbackMs;
  }
  const n = Number(raw);
  return Number.isFinite(n) ? n : fallbackMs;
}

function initButtonShine() {
  const root = document.documentElement;
  const cssDuration = getComputedStyle(root).getPropertyValue('--btn-shine-duration');
  btnShineTimeoutMs = parseDurationMs(cssDuration, 900) + 160;

  const getBtn = (target) => {
    if (!target || typeof target.closest !== 'function') return null;
    return target.closest('.btn');
  };

  const applyBtnShine = (btn, type) => {
    if (!btn || btn.disabled) return;
    const last = btnShineTimers.get(btn);
    if (last) clearTimeout(last);
    btn.classList.remove('btn-shine-in', 'btn-shine-out');
    void btn.offsetWidth;
    btn.classList.add(type);
    const timer = setTimeout(() => {
      btn.classList.remove(type);
      btnShineTimers.delete(btn);
    }, btnShineTimeoutMs);
    btnShineTimers.set(btn, timer);
  };

  document.addEventListener('pointerover', (e) => {
    const btn = getBtn(e.target);
    if (!btn) return;
    if (e.relatedTarget && btn.contains(e.relatedTarget)) return;
    applyBtnShine(btn, 'btn-shine-in');
  });

  document.addEventListener('pointerout', (e) => {
    const btn = getBtn(e.target);
    if (!btn) return;
    if (e.relatedTarget && btn.contains(e.relatedTarget)) return;
    applyBtnShine(btn, 'btn-shine-out');
  });
}

function setTheme(theme) {
  const t = theme === 'light' ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', t);
  const themeMeta = document.querySelector('meta[name="theme-color"]');
  if (themeMeta) themeMeta.setAttribute('content', t === 'light' ? '#eef3fa' : '#070b14');
  try { localStorage.setItem('bhb_theme', t); } catch (_) {}
  const btn = document.getElementById('theme-toggle');
  if (!btn) return;
  btn.innerHTML = t === 'light' ? THEME_ICON_SUN : THEME_ICON_MOON;
  btn.setAttribute('aria-label', t === 'light' ? '日间模式（点击切换夜间模式）' : '夜间模式（点击切换日间模式）');
  try {
    window.dispatchEvent(new CustomEvent('bhb-theme-change', { detail: { theme: t } }));
  } catch (_) {}
}

function initThemeToggle() {
  const btn = document.getElementById('theme-toggle');
  let initial = 'dark';
  try { initial = localStorage.getItem('bhb_theme') || 'dark'; } catch (_) {}
  setTheme(initial);
  if (btn) {
    btn.onclick = () => {
      const current = document.documentElement.getAttribute('data-theme') || 'dark';
      setTheme(current === 'light' ? 'dark' : 'light');
    };
  }
}

async function initVersionLabel() {
  const el = document.getElementById('brand-sub');
  if (!el) return;
  try {
    const cfg = await getConfig();
    const v = cfg && cfg.ui_version ? String(cfg.ui_version) : '--';
    el.textContent = `v${v}`;
  } catch (_) {}
}

function initRoutes() {
  registerRoute('#device', { pageId: 'page-device' });
  registerRoute('#mode', { pageId: 'page-mode' });
  registerRoute('#eeg', { pageId: 'page-eeg', onEnter: enterEegPage, onLeave: leaveEegPage });
  registerRoute('#offline', { pageId: 'page-offline', onEnter: enterOfflinePage, onLeave: leaveOfflinePage });
  registerRoute('#impedance', { pageId: 'page-impedance', onEnter: enterImpedancePage, onLeave: leaveImpedancePage });
  registerRoute('#tdcs', { pageId: 'page-tdcs', onEnter: enterTdcsPage, onLeave: leaveTdcsPage });
}


async function init() {
  // 窗口控制属于应用级能力，必须先于任何页面模块完成绑定。
  bindMinimizeButton();
  bindPowerButton();
  initDevicePage();
  initModePage();
  initRoutes();
  bindHeaderNav();
  window.addEventListener('app:music-lock', (event) => {
    musicLocked = !!event.detail;
    applyNavLock(lastStatusSnapshot?.device);
  });
  updateHeaderNavActive();
  window.addEventListener('hashchange', updateHeaderNavActive);
  initButtonShine();
  initThemeToggle();
  initVersionLabel();
  startStatusPolling();
  await initOptionalModules();
  startRouter();
}

init();
