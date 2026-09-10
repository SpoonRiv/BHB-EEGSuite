/*
Copyright (c) 2026 BUAA BHB. All rights reserved.

文件功能: 简单前端路由（hash 驱动页面显示 + 进入/离开钩子）
作者: Spoon
*/

import { setActivePage } from './ui.js';

const routes = new Map();
let currentHash = '';

export function registerRoute(hash, cfg) {
  routes.set(hash, cfg);
}

function normalizeHash(h) {
  const v = String(h || '').trim();
  if (!v) return '#device';
  if (v.startsWith('#')) return v;
  return `#${v}`;
}

export async function navigate(hash) {
  window.location.hash = normalizeHash(hash);
}

async function applyRoute(nextHash) {
  const nh = normalizeHash(nextHash);
  if (nh === currentHash) return;

  const next = routes.get(nh) || routes.get('#device');
  if (!next) return;
  if (typeof next.beforeEnter === 'function' && !await next.beforeEnter()) return;
  if (normalizeHash(window.location.hash) !== nh) return;

  const prev = routes.get(currentHash);
  if (prev && typeof prev.onLeave === 'function') {
    try { await prev.onLeave(); } catch (_) {}
  }

  currentHash = nh;
  setActivePage(next.pageId);
  if (typeof next.onEnter === 'function') {
    try { await next.onEnter(); } catch (error) {
      document.documentElement.dataset.routeError = String(error && (error.stack || error.message) || error);
      const diagnosticTarget = document.getElementById('btn-eeg-start');
      if (diagnosticTarget) diagnosticTarget.textContent = document.documentElement.dataset.routeError;
      console.error('route enter failed', error);
    }
  }
}

export function startRouter() {
  window.addEventListener('hashchange', () => { applyRoute(window.location.hash); });
  applyRoute(window.location.hash);
}
